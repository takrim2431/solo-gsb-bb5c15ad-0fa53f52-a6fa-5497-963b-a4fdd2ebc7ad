"""请求/响应模型。

所有时刻字段使用 AwareDatetime：缺少时区信息的请求会被 422 拒绝。
"""

from datetime import datetime

from pydantic import AwareDatetime, BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# 证书
# ---------------------------------------------------------------------------

class CertificateCreate(BaseModel):
    device_id: str = Field(min_length=1, max_length=128, description="设备编号")
    certificate_no: str = Field(min_length=1, max_length=128, description="证书编号")
    valid_from: AwareDatetime = Field(description="有效期起点（含，须带时区）")
    valid_to: AwareDatetime = Field(description="有效期终点（不含，须带时区）")

    @model_validator(mode="after")
    def _check_range(self) -> "CertificateCreate":
        if not self.valid_from < self.valid_to:
            raise ValueError("valid_from must be earlier than valid_to")
        return self


class CertificateOut(BaseModel):
    id: int
    device_id: str
    certificate_no: str
    valid_from: datetime
    valid_to: datetime
    revoked_at: datetime | None
    revoke_reason: str | None
    status: str = Field(description="active / revoked")
    created_at: datetime


class RevokeRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500, description="撤销原因")


# ---------------------------------------------------------------------------
# 测量记录
# ---------------------------------------------------------------------------

class MeasurementCreate(BaseModel):
    device_id: str = Field(min_length=1, max_length=128, description="设备编号")
    measured_at: AwareDatetime = Field(description="测量时刻（须带时区）")
    certificate_no: str = Field(min_length=1, max_length=128, description="证书编号")


class Validity(BaseModel):
    is_valid: bool
    reason: str


class MeasurementOut(BaseModel):
    id: int
    device_id: str
    measured_at: datetime
    certificate_no: str
    certificate_id: int
    created_at: datetime
    validity: Validity = Field(description="该记录按当前证书状态计算的实时有效性")


# ---------------------------------------------------------------------------
# 测量更正（追加式版本链）
# ---------------------------------------------------------------------------

class CorrectionCreate(BaseModel):
    base_version: int = Field(
        ge=1,
        description="当前最新版本号（乐观锁）：原始登记为 1，每次更正 +1；"
        "与最新版本不符时返回 409",
    )
    measured_at: AwareDatetime | None = Field(
        default=None, description="更正后的测量时刻（须带时区）；缺省沿用当前版本值"
    )
    certificate_no: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        description="更正后的证书编号（须属于该设备）；缺省沿用当前版本值",
    )
    reason: str = Field(min_length=1, max_length=500, description="更正原因")

    @model_validator(mode="after")
    def _check_at_least_one_field(self) -> "CorrectionCreate":
        if self.measured_at is None and self.certificate_no is None:
            raise ValueError("at least one of measured_at / certificate_no must be provided")
        return self


class MeasurementVersionOut(BaseModel):
    version: int = Field(description="版本号：原始登记为 1，每次更正 +1")
    device_id: str
    measured_at: datetime
    certificate_no: str
    certificate_id: int
    reason: str | None = Field(description="更正原因；原始版本为 null")
    created_at: datetime
    validity: Validity = Field(description="该版本按当前证书状态实时计算的有效性")


class MeasurementHistoryOut(BaseModel):
    measurement_id: int
    device_id: str
    original: MeasurementVersionOut = Field(description="原始版本（version=1）")
    corrections: list[MeasurementVersionOut] = Field(description="按版本号升序排列的更正链")
    current: MeasurementVersionOut = Field(description="当前生效版本（无更正时即原始版本）")
