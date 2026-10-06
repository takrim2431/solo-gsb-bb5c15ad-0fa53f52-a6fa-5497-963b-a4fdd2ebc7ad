"""测量记录批次写入：批次键幂等、逐条沿用单条写入校验、整批原子。"""

import hashlib
import json
from datetime import timezone

import asyncpg
from fastapi import APIRouter, Depends, Response

from ..db import get_pool
from ..errors import ApiError
from ..schemas import MeasurementBatchCreate, MeasurementBatchOut

router = APIRouter()


class _ReplaySuccess(Exception):
    """事务内撞上并发已提交的同键成功批次：回滚本事务后重放首次结果。"""

    def __init__(self, out: MeasurementBatchOut):
        self.out = out


def _canonical_hash(payload: MeasurementBatchCreate) -> str:
    """有序记录内容的规范化哈希。

    测量时刻统一换算为 UTC（同一时刻的不同时区写法视为相同内容）；
    记录按输入顺序参与哈希，顺序不同即不同内容。
    """
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


def _batch_error(index: int, code: str, message: str, status_code: int = 422) -> ApiError:
    """与单条写入一致的错误码，并附带第一条不合格记录的序号（0-based）。"""
    return ApiError(status_code, code, message, extra={"index": index})


def _replay(row) -> ApiError | MeasurementBatchOut:
    """把持久化的首次结果转换为重放响应（成功结果或失败异常）。"""
    if row["status"] == "succeeded":
        return MeasurementBatchOut(
            batch_key=row["batch_key"],
            measurement_ids=list(row["measurement_ids"]),
            created_at=row["created_at"],
        )
    return _batch_error(row["error_index"], row["error_code"], row["error_message"])


# 校验失败错误码集合：出现时需要把首次失败结果持久化
_VALIDATION_ERROR_CODES = {
    "CERTIFICATE_NOT_FOUND",
    "CERTIFICATE_REVOKED",
    "MEASUREMENT_TIME_NOT_COVERED",
}


@router.post("", response_model=MeasurementBatchOut, status_code=201)
async def create_measurement_batch(
    payload: MeasurementBatchCreate,
    response: Response,
    pool: asyncpg.Pool = Depends(get_pool),
) -> MeasurementBatchOut:
    """按批次写入测量记录。

    - 逐条沿用单条写入的证书归属、未撤销与左闭右开有效期校验；
      任一条不合格即返回其序号（0-based）与原因，且整批测量记录不落库；
    - 全部成功时返回与输入顺序一一对应的记录标识；记录仍不可修改；
    - 批次键幂等：相同键 + 相同有序内容（时刻按 UTC 比较）重试返回首次结果
      （成功 200，失败重放原状态码），即使引用的证书后来已撤销；
      相同键 + 不同内容返回 409 BATCH_KEY_CONFLICT；
    - 并发提交同一键至多生成一批（唯一约束保证），未决时返回 409 请稍后重试；
    - 校验在同一事务内对全部引用证书行加 FOR UPDATE 锁，
      与证书撤销并发时不会出现"通过校验却写入已撤销证书"的批次。
    """
    req_hash = _canonical_hash(payload)

    async with pool.acquire() as conn:
        # 幂等快路径：已有持久化结果则直接重放（不重新校验，证书撤销不影响）
        existing = await conn.fetchrow(
            "SELECT * FROM measurement_batches WHERE batch_key = $1", payload.batch_key
        )
        if existing is not None:
            if existing["request_hash"] != req_hash:
                raise ApiError(
                    409,
                    "BATCH_KEY_CONFLICT",
                    f"batch_key '{payload.batch_key}' was already used with "
                    "different ordered records",
                )
            replayed = _replay(existing)
            if isinstance(replayed, ApiError):
                raise replayed
            response.status_code = 200
            response.headers["Batch-Replayed"] = "true"
            return replayed

        try:
            async with conn.transaction():
                # 预取批次行 id：测量行的延迟外键在提交时才校验，
                # 批次行最后携带最终结果写入（批次表有不可变触发器，不能先占位再 UPDATE）
                batch_id = await conn.fetchval("SELECT nextval('measurement_batches_id_seq')")

                # 查出全部被引用的证书，再按证书 id 升序加行锁：
                # 固定加锁顺序避免多批次并发时死锁；行锁与撤销事务互斥，
                # 撤销必须等待本事务提交/回锁，故不会写入已撤销证书
                pairs = list({(r.device_id, r.certificate_no) for r in payload.records})
                found_rows = await conn.fetch(
                    """
                    SELECT c.*
                    FROM certificates c
                    JOIN unnest($1::text[], $2::text[])
                        AS t(device_id, certificate_no)
                      ON t.device_id = c.device_id
                     AND t.certificate_no = c.certificate_no
                    """,
                    [d for d, _ in pairs],
                    [c for _, c in pairs],
                )
                found = {(r["device_id"], r["certificate_no"]): r for r in found_rows}
                locked: dict[tuple[str, str], asyncpg.Record] = {}
                for key in sorted(found, key=lambda k: found[k]["id"]):
                    c = found[key]
                    locked[key] = await conn.fetchrow(
                        "SELECT * FROM certificates WHERE id = $1 FOR UPDATE", c["id"]
                    )

                # 逐条沿用单条写入校验：按输入顺序，返回第一条不合格记录的序号与原因
                cert_by_index: list[asyncpg.Record] = []
                for index, rec in enumerate(payload.records):
                    cert = locked.get((rec.device_id, rec.certificate_no))
                    if cert is None:
                        raise _batch_error(
                            index,
                            "CERTIFICATE_NOT_FOUND",
                            f"no certificate '{rec.certificate_no}' registered "
                            f"for device '{rec.device_id}'",
                        )
                    if cert["revoked_at"] is not None:
                        raise _batch_error(
                            index,
                            "CERTIFICATE_REVOKED",
                            f"certificate '{cert['certificate_no']}' was revoked at "
                            f"{cert['revoked_at'].isoformat()}: "
                            f"{cert['revoke_reason'] or ''}".rstrip(": "),
                        )
                    if not (cert["valid_from"] <= rec.measured_at < cert["valid_to"]):
                        raise _batch_error(
                            index,
                            "MEASUREMENT_TIME_NOT_COVERED",
                            f"certificate covers [{cert['valid_from'].isoformat()}, "
                            f"{cert['valid_to'].isoformat()}) but measurement time is "
                            f"{rec.measured_at.isoformat()}",
                        )
                    cert_by_index.append(cert)

                # 全部通过：按输入顺序写入（任一条插入失败则随事务回滚，整批不落库）
                measurement_ids: list[int] = []
                for rec, cert in zip(payload.records, cert_by_index, strict=True):
                    row = await conn.fetchrow(
                        "INSERT INTO measurements "
                        "(batch_id, device_id, measured_at, certificate_id) "
                        "VALUES ($1, $2, $3, $4) RETURNING id",
                        batch_id,
                        rec.device_id,
                        rec.measured_at,
                        cert["id"],
                    )
                    measurement_ids.append(row["id"])

                # 携带成功结果写入批次行；批次键唯一约束串行化并发提交（至多一批）
                try:
                    batch_row = await conn.fetchrow(
                        "INSERT INTO measurement_batches "
                        "(id, batch_key, request_hash, status, measurement_ids) "
                        "VALUES ($1, $2, $3, 'succeeded', $4) RETURNING *",
                        batch_id,
                        payload.batch_key,
                        req_hash,
                        measurement_ids,
                    )
                except asyncpg.UniqueViolationError:
                    racer = await conn.fetchrow(
                        "SELECT * FROM measurement_batches WHERE batch_key = $1",
                        payload.batch_key,
                    )
                    if racer is None:
                        # 并发同键请求尚未提交，稍候重试即可
                        raise ApiError(
                            409,
                            "BATCH_KEY_IN_FLIGHT",
                            "another submission with the same batch_key is in flight; "
                            "retry later",
                        )
                    if racer["request_hash"] != req_hash:
                        raise ApiError(
                            409,
                            "BATCH_KEY_CONFLICT",
                            f"batch_key '{payload.batch_key}' was already used with "
                            "different ordered records",
                        )
                    replayed = _replay(racer)
                    if isinstance(replayed, ApiError):
                        raise replayed
                    # 成功批次并发撞键：本事务回滚（重复测量行不落库），重放首次结果
                    raise _ReplaySuccess(replayed)
        except _ReplaySuccess as replay:
            response.status_code = 200
            response.headers["Batch-Replayed"] = "true"
            return replay.out
        except ApiError as exc:
            # 校验失败：事务已回滚（整批测量记录不落库，批次键未被占用），
            # 在同一连接上另开事务把首次失败结果持久化，供同键重放
            # （不二次申请连接，避免占满连接池后互相等待）
            if exc.code in _VALIDATION_ERROR_CODES:
                await _persist_failure(conn, payload.batch_key, req_hash, exc)
            raise

    return MeasurementBatchOut(
        batch_key=batch_row["batch_key"],
        measurement_ids=list(batch_row["measurement_ids"]),
        created_at=batch_row["created_at"],
    )


async def _persist_failure(
    conn: asyncpg.Connection, batch_key: str, req_hash: str, exc: ApiError
) -> None:
    """在前一事务回滚后，于同一连接的新事务中持久化首次失败结果。

    - 首次结果由本方写入；
    - 并发同键请求已先提交同内容结果：重放其持久化的首次结果；
    - 并发同键请求已先提交异内容结果：409 BATCH_KEY_CONFLICT；
    - 对方仍未决：放弃持久化，保留本次计算结果（调用方随后重试会重放）。
    """
    index = exc.extra["index"] if exc.extra else 0
    try:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO measurement_batches "
                "(batch_key, request_hash, status, error_index, error_code, error_message) "
                "VALUES ($1, $2, 'failed', $3, $4, $5)",
                batch_key,
                req_hash,
                index,
                exc.code,
                exc.message,
            )
    except asyncpg.UniqueViolationError:
        existing = await conn.fetchrow(
            "SELECT * FROM measurement_batches WHERE batch_key = $1", batch_key
        )
        if existing is None:
            return
        if existing["request_hash"] != req_hash:
            raise ApiError(
                409,
                "BATCH_KEY_CONFLICT",
                f"batch_key '{batch_key}' was already used with "
                "different ordered records",
            )
        replayed = _replay(existing)
        if isinstance(replayed, ApiError):
            raise replayed
