-- 实验室设备校准追溯服务 —— 数据库初始化脚本
-- 由 docker-compose 挂载到 postgres 容器的 /docker-entrypoint-initdb.d/ 自动执行

-- exclusion 约束需要 btree_gist（text = text 的 GiST 操作符）
CREATE EXTENSION IF NOT EXISTS btree_gist;

-- ---------------------------------------------------------------------------
-- 证书表
--   * 有效期区间左闭右开 [valid_from, valid_to)，起点必须早于终点
--   * 同一设备任意两份证书的有效期不得重叠（exclusion 约束，并发下只有一份成功）
--   * 幂等键全局唯一，配合 request_hash 识别"同键异内容"
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS certificates (
    id              BIGSERIAL    PRIMARY KEY,
    idempotency_key TEXT         NOT NULL,
    request_hash    TEXT         NOT NULL,
    device_id       TEXT         NOT NULL,
    certificate_no  TEXT         NOT NULL,
    valid_from      TIMESTAMPTZ  NOT NULL,
    valid_to        TIMESTAMPTZ  NOT NULL,
    revoked_at      TIMESTAMPTZ,
    revoke_reason   TEXT,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),

    CONSTRAINT certificates_valid_range_chk CHECK (valid_from < valid_to),
    CONSTRAINT certificates_idempotency_key_uq UNIQUE (idempotency_key),
    CONSTRAINT certificates_device_no_uq UNIQUE (device_id, certificate_no),
    CONSTRAINT certificates_no_overlap_excl EXCLUDE USING gist (
        device_id WITH =,
        tstzrange(valid_from, valid_to, '[)') WITH &&
    )
);

CREATE INDEX IF NOT EXISTS certificates_device_idx ON certificates (device_id);

-- ---------------------------------------------------------------------------
-- 测量记录表：一经写入不可修改、不可删除（触发器强制）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS measurements (
    id             BIGSERIAL    PRIMARY KEY,
    device_id      TEXT         NOT NULL,
    measured_at    TIMESTAMPTZ  NOT NULL,
    certificate_id BIGINT       NOT NULL REFERENCES certificates (id),
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS measurements_device_idx ON measurements (device_id);
CREATE INDEX IF NOT EXISTS measurements_cert_idx   ON measurements (certificate_id);

CREATE OR REPLACE FUNCTION reject_measurement_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'measurements are immutable: UPDATE/DELETE not allowed';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS measurements_immutable_trg ON measurements;
CREATE TRIGGER measurements_immutable_trg
    BEFORE UPDATE OR DELETE ON measurements
    FOR EACH ROW EXECUTE FUNCTION reject_measurement_mutation();

-- ---------------------------------------------------------------------------
-- 测量更正表：追加式更正链
--   * 原始登记为 version 1，每次更正追加一行，version 单调 +1（CHECK >= 2）
--   * (measurement_id, version) 唯一：同一版本的并发更正最多成功一次
--   * 只增不改：与测量表一样挂不可变触发器，任何版本不得修改或删除
--   * 设备编号不在本表：更正不改变设备，查询时从原始测量记录取得
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS measurement_corrections (
    id              BIGSERIAL    PRIMARY KEY,
    measurement_id  BIGINT       NOT NULL REFERENCES measurements (id),
    version         INT          NOT NULL,
    measured_at     TIMESTAMPTZ  NOT NULL,
    certificate_id  BIGINT       NOT NULL REFERENCES certificates (id),
    reason          TEXT         NOT NULL,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),

    CONSTRAINT measurement_corrections_version_chk CHECK (version >= 2),
    CONSTRAINT measurement_corrections_version_uq UNIQUE (measurement_id, version)
);

CREATE INDEX IF NOT EXISTS measurement_corrections_measurement_idx
    ON measurement_corrections (measurement_id);

CREATE OR REPLACE FUNCTION reject_correction_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'measurement corrections are immutable: UPDATE/DELETE not allowed';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS measurement_corrections_immutable_trg ON measurement_corrections;
CREATE TRIGGER measurement_corrections_immutable_trg
    BEFORE UPDATE OR DELETE ON measurement_corrections
    FOR EACH ROW EXECUTE FUNCTION reject_correction_mutation();
