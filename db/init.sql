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
-- 测量批次表：一次上传一批有序测量记录的幂等登记
--   * batch_key 全局唯一：相同键的并发提交最多成功一批
--   * request_hash 为有序内容的规范化哈希（测量时刻统一换算 UTC），
--     同键异内容返回 409
--   * response_json 保存首次成功的完整响应快照：同键同内容重放时原样返回，
--     即使引用的证书后来被撤销
--   * 批次行只增不改：写入事务内先取批次序列 id、插入本批测量记录，最后一次性
--     插入本行（携带响应快照），快照一旦写入即终态，无需事后 UPDATE
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS measurement_batches (
    id            BIGSERIAL    PRIMARY KEY,
    batch_key     TEXT         NOT NULL,
    request_hash  TEXT         NOT NULL,
    record_count  INT          NOT NULL,
    response_json JSONB        NOT NULL,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),

    CONSTRAINT measurement_batches_count_chk CHECK (record_count >= 1),
    CONSTRAINT measurement_batches_key_uq UNIQUE (batch_key)
);

CREATE OR REPLACE FUNCTION reject_batch_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'measurement batches are immutable: UPDATE/DELETE not allowed';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS measurement_batches_immutable_trg ON measurement_batches;
CREATE TRIGGER measurement_batches_immutable_trg
    BEFORE UPDATE OR DELETE ON measurement_batches
    FOR EACH ROW EXECUTE FUNCTION reject_batch_mutation();

-- ---------------------------------------------------------------------------
-- 测量记录表：一经写入不可修改、不可删除（触发器强制）
--   * 单条写入时 batch_id / position 均为 NULL
--   * 批次写入时 batch_id 指向 measurement_batches，position 为输入中的
--     从 1 开始的序号；(batch_id, position) 唯一
--   * 指向批次表的外键为 DEFERRABLE INITIALLY DEFERRED：批次事务先取批次
--     序列 id 并插入本批测量记录，最后才插入批次行（携带响应快照），
--     外键在提交时检查，保证"有批次行必有快照"且批次行无需事后更新
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS measurements (
    id             BIGSERIAL    PRIMARY KEY,
    device_id      TEXT         NOT NULL,
    measured_at    TIMESTAMPTZ  NOT NULL,
    certificate_id BIGINT       NOT NULL REFERENCES certificates (id),
    batch_id       BIGINT,
    position       INT,
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),

    CONSTRAINT measurements_batch_fk
        FOREIGN KEY (batch_id) REFERENCES measurement_batches (id)
        DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT measurements_batch_position_chk CHECK (
        (batch_id IS NULL AND position IS NULL)
        OR (batch_id IS NOT NULL AND position IS NOT NULL AND position >= 1)
    )
);

CREATE INDEX IF NOT EXISTS measurements_device_idx ON measurements (device_id);
CREATE INDEX IF NOT EXISTS measurements_cert_idx   ON measurements (certificate_id);
CREATE UNIQUE INDEX IF NOT EXISTS measurements_batch_position_uq
    ON measurements (batch_id, position) WHERE batch_id IS NOT NULL;

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
