"""测量记录批量写入：整批校验、整批落库，批次键幂等。"""

import hashlib
import json
from datetime import timezone

import asyncpg
from fastapi import APIRouter, Depends, Response

from ..db import get_pool
from ..errors import ApiError
from ..schemas import MeasurementBatchCreate, MeasurementBatchOut, MeasurementOut, Validity

router = APIRouter()


def _canonical_hash(payload: MeasurementBatchCreate) -> str:
    """有序内容的规范化哈希：测量时刻统一换算为 UTC，时刻的不同时区写法不影响判定。"""
    canonical = [
        {
            "device_id": r.device_id,
            "certificate_no": r.certificate_no,
            "measured_at": r.measured_at.astimezone(timezone.utc).isoformat(),
        }
        for r in payload.records
    ]
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _validity(revoked_at, revoke_reason) -> Validity:
    # 与单条写入保持一致的判定：批次提交快照时证书必为未撤销状态
    if revoked_at is not None:
        reason = f"certificate revoked at {revoked_at.isoformat()}"
        if revoke_reason:
            reason += f": {revoke_reason}"
        return Validity(is_valid=False, reason=reason)
    return Validity(
        is_valid=True,
        reason="certificate is active and covers the measurement time",
    )


def _record_error(index: int, code: str, message: str) -> ApiError:
    """整批拒绝：422 + 失败序号（从 1 开始）与逐条沿用的单条写入错误码。"""
    return ApiError(
        422,
        code,
        f"record at index {index} rejected: {message}",
        details={"index": index},
    )


def _cert_revoked_message(cert) -> str:
    return (
        f"certificate '{cert['certificate_no']}' was revoked at "
        f"{cert['revoked_at'].isoformat()}: {cert['revoke_reason'] or ''}".rstrip(": ")
    )


def _replay(existing, response: Response) -> MeasurementBatchOut:
    response.status_code = 200
    response.headers["Idempotency-Replayed"] = "true"
    # 直接返回首次结果快照：不再触碰证书状态，故撤销后重放结果不变
    return MeasurementBatchOut.model_validate(json.loads(existing["response_json"]))


@router.post("", response_model=MeasurementBatchOut, status_code=201)
async def create_measurement_batch(
    payload: MeasurementBatchCreate,
    response: Response,
    pool: asyncpg.Pool = Depends(get_pool),
) -> MeasurementBatchOut:
    """批量写入测量记录。

    - 逐条沿用单条写入的校验：证书须属于该设备、未撤销，且左闭右开覆盖测量时刻；
      任一条不合格则返回其序号与原因，**整批回滚、不落库**。
    - 全部合格时整批写入，返回与输入顺序一一对应的记录标识；记录一经写入不可修改。
    - 幂等：相同批次键 + 相同有序内容（时刻按 UTC 比较）重试 → 200 原样返回首次
      结果（响应头 `Idempotency-Replayed: true`），即使证书后来被撤销；
      相同键 + 不同内容 → 409；相同键的并发提交最多生成一批。
    - 与证书撤销并发时：先按证书 id 顺序对本批涉及的证书行加 FOR UPDATE 锁
      再逐条校验，与撤销事务互斥，不会出现通过校验却写入已撤销证书的批次。
    """
    req_hash = _canonical_hash(payload)
    records = payload.records

    async with pool.acquire() as conn:
        try:
            return await _process_batch(conn, payload, req_hash, records, response)
        except asyncpg.UniqueViolationError:
            # 并发同键：赢家恰好在本事务校验/写入期间提交（绕过咨询锁的极小窗口兜底）。
            # 取咨询锁后重取一次，此时赢家必已提交完成。
            async with conn.transaction():
                await conn.fetchval(
                    "SELECT pg_advisory_xact_lock(hashtext($1))", payload.batch_key
                )
                existing = await conn.fetchrow(
                    "SELECT * FROM measurement_batches WHERE batch_key = $1",
                    payload.batch_key,
                )
                if existing is None:
                    raise ApiError(
                        409,
                        "IDEMPOTENCY_IN_FLIGHT",
                        "another request with the same batch_key is in flight; retry later",
                    )
                if existing["request_hash"] != req_hash:
                    raise ApiError(
                        409,
                        "IDEMPOTENCY_KEY_CONFLICT",
                        "batch_key was already used with a different set of ordered records",
                    )
                return _replay(existing, response)


async def _process_batch(conn, payload, req_hash, records, response) -> MeasurementBatchOut:
    async with conn.transaction():
        # 咨询锁：按 batch_key 串行化同键事务。
        # 仅靠 SELECT ... FOR UPDATE 不够——READ COMMITTED 下它不会等待
        # 语句开始后由其他事务新插入（而非更新）的批次行。
        await conn.fetchval(
            "SELECT pg_advisory_xact_lock(hashtext($1))", payload.batch_key
        )
        existing = await conn.fetchrow(
            "SELECT * FROM measurement_batches WHERE batch_key = $1",
            payload.batch_key,
        )
        if existing is not None:
            if existing["request_hash"] != req_hash:
                raise ApiError(
                    409,
                    "IDEMPOTENCY_KEY_CONFLICT",
                    "batch_key was already used with a different set of ordered records",
                )
            return _replay(existing, response)

        # 预取批次 id：测量记录先插、批次行最后插，外键 DEFERRABLE 到提交时检查
        batch_id = await conn.fetchval("SELECT nextval('measurement_batches_id_seq')")

        # 一次性锁定本批涉及的全部证书行（按 id 全局排序，避免多批并发时锁序死锁），
        # 与单条写入/撤销同样的 FOR UPDATE 语义，且逐设备去重
        pairs = list({(r.device_id, r.certificate_no) for r in records})
        cert_rows = await conn.fetch(
            "SELECT c.* FROM certificates c "
            "JOIN unnest($1::text[], $2::text[]) AS p(device_id, certificate_no) "
            "ON p.device_id = c.device_id AND p.certificate_no = c.certificate_no "
            "ORDER BY c.id FOR UPDATE OF c",
            [p[0] for p in pairs],
            [p[1] for p in pairs],
        )
        certs = {(r["device_id"], r["certificate_no"]): r for r in cert_rows}

        # 逐条校验并写入：任何一条不合格都抛错，事务回滚，整批不落库
        out_records: list[MeasurementOut] = []
        record_ids: list[int] = []
        for i, item in enumerate(records, start=1):
            cert = certs.get((item.device_id, item.certificate_no))
            if cert is None:
                raise _record_error(
                    i,
                    "CERTIFICATE_NOT_FOUND",
                    f"no certificate '{item.certificate_no}' registered "
                    f"for device '{item.device_id}'",
                )
            if cert["revoked_at"] is not None:
                raise _record_error(i, "CERTIFICATE_REVOKED", _cert_revoked_message(cert))
            if not (cert["valid_from"] <= item.measured_at < cert["valid_to"]):
                raise _record_error(
                    i,
                    "MEASUREMENT_TIME_NOT_COVERED",
                    f"certificate covers [{cert['valid_from'].isoformat()}, "
                    f"{cert['valid_to'].isoformat()}) but measurement time is "
                    f"{item.measured_at.isoformat()}",
                )
            row = await conn.fetchrow(
                "INSERT INTO measurements "
                "(device_id, measured_at, certificate_id, batch_id, position) "
                "VALUES ($1, $2, $3, $4, $5) RETURNING *",
                item.device_id,
                item.measured_at,
                cert["id"],
                batch_id,
                i,
            )
            record_ids.append(row["id"])
            out_records.append(
                MeasurementOut(
                    id=row["id"],
                    device_id=row["device_id"],
                    measured_at=row["measured_at"],
                    certificate_no=cert["certificate_no"],
                    certificate_id=row["certificate_id"],
                    created_at=row["created_at"],
                    validity=_validity(cert["revoked_at"], cert["revoke_reason"]),
                )
            )

        snapshot = MeasurementBatchOut(
            batch_key=payload.batch_key,
            record_count=len(records),
            record_ids=record_ids,
            records=out_records,
            created_at=out_records[0].created_at,
        )
        batch_row = await conn.fetchrow(
            "INSERT INTO measurement_batches "
            "(id, batch_key, request_hash, record_count, response_json) "
            "VALUES ($1, $2, $3, $4, $5::jsonb) RETURNING created_at",
            batch_id,
            payload.batch_key,
            req_hash,
            len(records),
            snapshot.model_dump_json(),
        )
        # 以批次行创建时刻为权威值（与首条记录同一事务，时刻一致）
        return snapshot.model_copy(update={"created_at": batch_row["created_at"]})
