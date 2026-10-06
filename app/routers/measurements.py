"""测量记录写入 / 查询（含实时有效性判定）/ 追加式更正。"""

import asyncpg
from fastapi import APIRouter, Depends, Query

from ..db import get_pool
from ..errors import ApiError
from ..schemas import (
    CorrectionCreate,
    MeasurementCreate,
    MeasurementHistoryOut,
    MeasurementOut,
    MeasurementVersionOut,
    Validity,
)

router = APIRouter()

# 关联证书当前状态，有效性在查询时实时计算（撤销立即反映到历史记录）
_JOIN_SQL = """
SELECT m.id, m.device_id, m.measured_at, m.certificate_id, m.created_at,
       c.certificate_no, c.revoked_at, c.revoke_reason
FROM measurements m
JOIN certificates c ON c.id = m.certificate_id
"""


def _validity(revoked_at, revoke_reason) -> Validity:
    if revoked_at is not None:
        reason = f"certificate revoked at {revoked_at.isoformat()}"
        if revoke_reason:
            reason += f": {revoke_reason}"
        return Validity(is_valid=False, reason=reason)
    return Validity(
        is_valid=True,
        reason="certificate is active and covers the measurement time",
    )


def _to_out(row) -> MeasurementOut:
    return MeasurementOut(
        id=row["id"],
        device_id=row["device_id"],
        measured_at=row["measured_at"],
        certificate_no=row["certificate_no"],
        certificate_id=row["certificate_id"],
        created_at=row["created_at"],
        validity=_validity(row["revoked_at"], row["revoke_reason"]),
    )


@router.post("", response_model=MeasurementOut, status_code=201)
async def create_measurement(
    payload: MeasurementCreate,
    pool: asyncpg.Pool = Depends(get_pool),
) -> MeasurementOut:
    """写入测量记录。

    仅当指定证书存在、覆盖测量时刻（左闭右开）且未撤销时接受；
    记录一经写入不可修改（数据库触发器强制）。
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            # 行锁防止证书在校验与写入之间被并发撤销
            cert = await conn.fetchrow(
                "SELECT * FROM certificates WHERE device_id = $1 AND certificate_no = $2 "
                "FOR UPDATE",
                payload.device_id,
                payload.certificate_no,
            )
            if cert is None:
                raise ApiError(
                    422,
                    "CERTIFICATE_NOT_FOUND",
                    f"no certificate '{payload.certificate_no}' registered "
                    f"for device '{payload.device_id}'",
                )
            if cert["revoked_at"] is not None:
                raise ApiError(
                    422,
                    "CERTIFICATE_REVOKED",
                    f"certificate '{payload.certificate_no}' was revoked at "
                    f"{cert['revoked_at'].isoformat()}: {cert['revoke_reason'] or ''}".rstrip(": "),
                )
            if not (cert["valid_from"] <= payload.measured_at < cert["valid_to"]):
                raise ApiError(
                    422,
                    "MEASUREMENT_TIME_NOT_COVERED",
                    f"certificate covers [{cert['valid_from'].isoformat()}, "
                    f"{cert['valid_to'].isoformat()}) but measurement time is "
                    f"{payload.measured_at.isoformat()}",
                )
            row = await conn.fetchrow(
                "INSERT INTO measurements (device_id, measured_at, certificate_id) "
                "VALUES ($1, $2, $3) RETURNING *",
                payload.device_id,
                payload.measured_at,
                cert["id"],
            )
    return MeasurementOut(
        id=row["id"],
        device_id=row["device_id"],
        measured_at=row["measured_at"],
        certificate_no=cert["certificate_no"],
        certificate_id=row["certificate_id"],
        created_at=row["created_at"],
        validity=_validity(cert["revoked_at"], cert["revoke_reason"]),
    )


@router.get("", response_model=list[MeasurementOut])
async def list_measurements(
    device_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    pool: asyncpg.Pool = Depends(get_pool),
) -> list[MeasurementOut]:
    rows = await pool.fetch(
        _JOIN_SQL
        + " WHERE ($1::text IS NULL OR m.device_id = $1) ORDER BY m.id LIMIT $2 OFFSET $3",
        device_id,
        limit,
        offset,
    )
    return [_to_out(r) for r in rows]


@router.get("/{measurement_id}", response_model=MeasurementOut)
async def get_measurement(
    measurement_id: int,
    pool: asyncpg.Pool = Depends(get_pool),
) -> MeasurementOut:
    row = await pool.fetchrow(_JOIN_SQL + " WHERE m.id = $1", measurement_id)
    if row is None:
        raise ApiError(404, "MEASUREMENT_NOT_FOUND", f"measurement id={measurement_id} not found")
    return _to_out(row)


# ---------------------------------------------------------------------------
# 更正：追加式版本链（原始登记 version=1，每次更正 +1，任何版本不可改删）
# ---------------------------------------------------------------------------

# 单版本视图：原始版本取自 measurements，更正版本取自 measurement_corrections，
# 统一 JOIN 证书当前状态，有效性实时计算
def _version_out(version: int, device_id: str, row, reason: str | None) -> MeasurementVersionOut:
    return MeasurementVersionOut(
        version=version,
        device_id=device_id,
        measured_at=row["measured_at"],
        certificate_no=row["certificate_no"],
        certificate_id=row["certificate_id"],
        reason=reason,
        created_at=row["created_at"],
        validity=_validity(row["revoked_at"], row["revoke_reason"]),
    )


_CORRECTION_JOIN_SQL = """
SELECT mc.id, mc.version, mc.measured_at, mc.certificate_id, mc.reason, mc.created_at,
       c.certificate_no, c.revoked_at, c.revoke_reason
FROM measurement_corrections mc
JOIN certificates c ON c.id = mc.certificate_id
"""


@router.post("/{measurement_id}/corrections", response_model=MeasurementVersionOut, status_code=201)
async def create_correction(
    measurement_id: int,
    payload: CorrectionCreate,
    pool: asyncpg.Pool = Depends(get_pool),
) -> MeasurementVersionOut:
    """提交更正：修正测量时刻和/或所用证书，必须携带原因与当前版本号。

    - 设备编号不可更正，始终沿用原始记录；
    - 仅从最新版本继续（base_version 须等于当前最新版本号），否则 409 VERSION_CONFLICT；
      同一版本的并发更正由行锁 + (measurement_id, version) 唯一约束保证最多成功一次；
    - 所选证书必须属于该设备、覆盖新时刻且提交时未撤销；
      校验与写入在同一事务内并对证书行加 FOR UPDATE 锁，防止并发撤销造成无效更正；
    - 更正以追加方式保存为新版本，原始记录与历次更正均不可修改、不可删除。
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            # 锁测量记录行：串行化同一记录的并发更正
            m = await conn.fetchrow(
                "SELECT * FROM measurements WHERE id = $1 FOR UPDATE", measurement_id
            )
            if m is None:
                raise ApiError(
                    404, "MEASUREMENT_NOT_FOUND", f"measurement id={measurement_id} not found"
                )
            latest = await conn.fetchrow(
                "SELECT version, measured_at, certificate_id FROM measurement_corrections "
                "WHERE measurement_id = $1 ORDER BY version DESC LIMIT 1",
                measurement_id,
            )
            if latest is None:
                current_version, cur_measured_at, cur_cert_id = (
                    1,
                    m["measured_at"],
                    m["certificate_id"],
                )
            else:
                current_version, cur_measured_at, cur_cert_id = (
                    latest["version"],
                    latest["measured_at"],
                    latest["certificate_id"],
                )
            if payload.base_version != current_version:
                raise ApiError(
                    409,
                    "VERSION_CONFLICT",
                    f"base_version {payload.base_version} does not match current version "
                    f"{current_version}; reload the latest version and retry",
                )

            new_measured_at = (
                payload.measured_at if payload.measured_at is not None else cur_measured_at
            )
            # 行锁防止证书在校验与写入之间被并发撤销
            if payload.certificate_no is not None:
                cert = await conn.fetchrow(
                    "SELECT * FROM certificates WHERE device_id = $1 AND certificate_no = $2 "
                    "FOR UPDATE",
                    m["device_id"],
                    payload.certificate_no,
                )
                if cert is None:
                    raise ApiError(
                        422,
                        "CERTIFICATE_NOT_FOUND",
                        f"no certificate '{payload.certificate_no}' registered "
                        f"for device '{m['device_id']}'",
                    )
            else:
                cert = await conn.fetchrow(
                    "SELECT * FROM certificates WHERE id = $1 FOR UPDATE", cur_cert_id
                )
            if cert["revoked_at"] is not None:
                raise ApiError(
                    422,
                    "CERTIFICATE_REVOKED",
                    f"certificate '{cert['certificate_no']}' was revoked at "
                    f"{cert['revoked_at'].isoformat()}: {cert['revoke_reason'] or ''}".rstrip(": "),
                )
            if not (cert["valid_from"] <= new_measured_at < cert["valid_to"]):
                raise ApiError(
                    422,
                    "MEASUREMENT_TIME_NOT_COVERED",
                    f"certificate covers [{cert['valid_from'].isoformat()}, "
                    f"{cert['valid_to'].isoformat()}) but measurement time is "
                    f"{new_measured_at.isoformat()}",
                )
            try:
                row = await conn.fetchrow(
                    "INSERT INTO measurement_corrections "
                    "(measurement_id, version, measured_at, certificate_id, reason) "
                    "VALUES ($1, $2, $3, $4, $5) RETURNING *",
                    measurement_id,
                    current_version + 1,
                    new_measured_at,
                    cert["id"],
                    payload.reason,
                )
            except asyncpg.UniqueViolationError:
                # 兜底：同一版本的并发更正最多成功一次
                raise ApiError(
                    409,
                    "VERSION_CONFLICT",
                    f"version {current_version + 1} was already created concurrently; "
                    "reload the latest version and retry",
                )
    return MeasurementVersionOut(
        version=row["version"],
        device_id=m["device_id"],
        measured_at=row["measured_at"],
        certificate_no=cert["certificate_no"],
        certificate_id=row["certificate_id"],
        reason=row["reason"],
        created_at=row["created_at"],
        validity=_validity(cert["revoked_at"], cert["revoke_reason"]),
    )


@router.get("/{measurement_id}/history", response_model=MeasurementHistoryOut)
async def get_measurement_history(
    measurement_id: int,
    pool: asyncpg.Pool = Depends(get_pool),
) -> MeasurementHistoryOut:
    """完整历史：原始版本 + 按版本号升序的更正链 + 当前版本。

    所有版本（含当前版本）的有效性均按证书撤销状态实时重算；旧版历史保留可查。
    """
    m = await pool.fetchrow(_JOIN_SQL + " WHERE m.id = $1", measurement_id)
    if m is None:
        raise ApiError(404, "MEASUREMENT_NOT_FOUND", f"measurement id={measurement_id} not found")
    rows = await pool.fetch(
        _CORRECTION_JOIN_SQL + " WHERE mc.measurement_id = $1 ORDER BY mc.version",
        measurement_id,
    )
    original = _version_out(1, m["device_id"], m, reason=None)
    corrections = [
        _version_out(r["version"], m["device_id"], r, reason=r["reason"]) for r in rows
    ]
    return MeasurementHistoryOut(
        measurement_id=measurement_id,
        device_id=m["device_id"],
        original=original,
        corrections=corrections,
        current=corrections[-1] if corrections else original,
    )
