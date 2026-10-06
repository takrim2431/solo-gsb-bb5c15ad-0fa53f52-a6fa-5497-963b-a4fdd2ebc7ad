"""证书登记 / 查询 / 撤销。"""

import hashlib
import json
from datetime import timezone

import asyncpg
from fastapi import APIRouter, Depends, Header, Query, Response

from ..db import get_pool
from ..errors import ApiError
from ..schemas import CertificateCreate, CertificateOut, RevokeRequest

router = APIRouter()

_INSERT_SQL = """
INSERT INTO certificates (idempotency_key, request_hash, device_id, certificate_no, valid_from, valid_to)
VALUES ($1, $2, $3, $4, $5, $6)
RETURNING *
"""


def _canonical_hash(payload: CertificateCreate) -> str:
    """请求内容的规范化哈希：时刻统一换算为 UTC，避免同一时刻不同时区写法被误判为异内容。"""
    canonical = {
        "device_id": payload.device_id,
        "certificate_no": payload.certificate_no,
        "valid_from": payload.valid_from.astimezone(timezone.utc).isoformat(),
        "valid_to": payload.valid_to.astimezone(timezone.utc).isoformat(),
    }
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _to_out(row) -> CertificateOut:
    return CertificateOut(
        id=row["id"],
        device_id=row["device_id"],
        certificate_no=row["certificate_no"],
        valid_from=row["valid_from"],
        valid_to=row["valid_to"],
        revoked_at=row["revoked_at"],
        revoke_reason=row["revoke_reason"],
        status="revoked" if row["revoked_at"] is not None else "active",
        created_at=row["created_at"],
    )


@router.post("", response_model=CertificateOut, status_code=201)
async def register_certificate(
    payload: CertificateCreate,
    response: Response,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=255),
    pool: asyncpg.Pool = Depends(get_pool),
) -> CertificateOut:
    """登记证书。

    - 有效期区间左闭右开 [valid_from, valid_to)，同设备区间重叠 → 409；
      重叠检测由数据库 exclusion 约束保证，并发提交只有一份成功。
    - 幂等：相同 Idempotency-Key + 相同内容重试 → 200 返回首次登记结果；
      相同键 + 不同内容 → 409。
    """
    req_hash = _canonical_hash(payload)
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                _INSERT_SQL,
                idempotency_key,
                req_hash,
                payload.device_id,
                payload.certificate_no,
                payload.valid_from,
                payload.valid_to,
            )
        except asyncpg.ExclusionViolationError:
            raise ApiError(
                409,
                "VALIDITY_OVERLAP",
                f"device '{payload.device_id}' already has a certificate whose "
                f"validity range overlaps [{payload.valid_from.isoformat()}, "
                f"{payload.valid_to.isoformat()})",
            )
        except asyncpg.UniqueViolationError as exc:
            if exc.constraint_name == "certificates_idempotency_key_uq":
                existing = await conn.fetchrow(
                    "SELECT * FROM certificates WHERE idempotency_key = $1", idempotency_key
                )
                if existing is None:
                    # 并发同键请求尚未提交，稍候重试即可
                    raise ApiError(
                        409,
                        "IDEMPOTENCY_IN_FLIGHT",
                        "another request with the same Idempotency-Key is in flight; retry later",
                    )
                if existing["request_hash"] != req_hash:
                    raise ApiError(
                        409,
                        "IDEMPOTENCY_KEY_CONFLICT",
                        "Idempotency-Key was already used with a different payload",
                    )
                response.status_code = 200
                response.headers["Idempotency-Replayed"] = "true"
                return _to_out(existing)
            if exc.constraint_name == "certificates_device_no_uq":
                raise ApiError(
                    409,
                    "DUPLICATE_CERTIFICATE_NO",
                    f"certificate_no '{payload.certificate_no}' is already registered "
                    f"for device '{payload.device_id}'",
                )
            raise
    return _to_out(row)


@router.get("", response_model=list[CertificateOut])
async def list_certificates(
    device_id: str | None = Query(default=None),
    status: str | None = Query(default=None, pattern="^(active|revoked)$"),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    pool: asyncpg.Pool = Depends(get_pool),
) -> list[CertificateOut]:
    sql = "SELECT * FROM certificates WHERE ($1::text IS NULL OR device_id = $1)"
    if status == "active":
        sql += " AND revoked_at IS NULL"
    elif status == "revoked":
        sql += " AND revoked_at IS NOT NULL"
    sql += " ORDER BY id LIMIT $2 OFFSET $3"
    rows = await pool.fetch(sql, device_id, limit, offset)
    return [_to_out(r) for r in rows]


@router.get("/{certificate_id}", response_model=CertificateOut)
async def get_certificate(
    certificate_id: int,
    pool: asyncpg.Pool = Depends(get_pool),
) -> CertificateOut:
    row = await pool.fetchrow("SELECT * FROM certificates WHERE id = $1", certificate_id)
    if row is None:
        raise ApiError(404, "CERTIFICATE_NOT_FOUND", f"certificate id={certificate_id} not found")
    return _to_out(row)


@router.post("/{certificate_id}/revoke", response_model=CertificateOut)
async def revoke_certificate(
    certificate_id: int,
    payload: RevokeRequest,
    pool: asyncpg.Pool = Depends(get_pool),
) -> CertificateOut:
    """撤销证书：只标记 revoked_at，不删除任何历史测量记录。"""
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM certificates WHERE id = $1 FOR UPDATE", certificate_id
            )
            if row is None:
                raise ApiError(404, "CERTIFICATE_NOT_FOUND", f"certificate id={certificate_id} not found")
            if row["revoked_at"] is not None:
                raise ApiError(
                    409,
                    "CERTIFICATE_ALREADY_REVOKED",
                    f"certificate id={certificate_id} was already revoked at "
                    f"{row['revoked_at'].isoformat()}",
                )
            row = await conn.fetchrow(
                "UPDATE certificates SET revoked_at = now(), revoke_reason = $2 "
                "WHERE id = $1 RETURNING *",
                certificate_id,
                payload.reason,
            )
    return _to_out(row)
