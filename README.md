# 实验室设备校准追溯服务

基于 **FastAPI + PostgreSQL** 的校准证书登记与测量记录追溯服务。

- 登记设备校准证书：编号、有效起止时刻（**必须带时区**，起点 < 终点，区间**左闭右开** `[valid_from, valid_to)`）
- 同一设备证书有效期**不得重叠**：由数据库 exclusion 约束保证，**并发提交只有一份成功**，其余返回 409
- 测量记录：仅当证书**覆盖测量时刻且未撤销**时才接受写入；记录**一经写入不可修改**（数据库触发器强制）
- 测量批次：一次上传**批次键 + 有序记录**，逐条沿用单条写入校验，**任一条不合格即整批回滚不落库**；同键同内容（时刻按 UTC 比较）重试返回**首次结果快照**（即使证书后来撤销），同键异内容 409，并发同键最多一批
- 测量更正：测量时刻或证书填错时可提交**带原因的更正**，原始记录与历次更正**只追加、不删改**；每次更正须携带当前版本号，**版本不符返回 409，同一版本的并发更正最多成功一次**
- 撤销证书**不删除**历史测量；查询时**实时**返回每条记录的当前有效性及原因
- 证书登记支持**幂等键**：相同键 + 相同内容重试返回首次结果；相同键 + 不同内容返回 409

## 一键启动

```bash
docker compose up --build
```

启动后：

| 组件 | 地址 |
|---|---|
| HTTP API | http://localhost:8000 |
| 交互式文档 (Swagger UI) | http://localhost:8000/docs |
| 健康检查 | http://localhost:8000/health |
| PostgreSQL | localhost:5432（用户/密码 `postgres`/`postgres`，库名 `calibration`） |

数据库首次启动时自动执行 `db/init.sql` 完成建表初始化；API 容器等待数据库健康检查后启动，自身也带连接重试。

停止并清空数据：`docker compose down -v`

## 配置

通过环境变量覆盖（见 `app/config.py`）：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DATABASE_URL` | `postgresql://postgres:postgres@localhost:5432/calibration` | 数据库连接串 |
| `DB_POOL_MIN_SIZE` / `DB_POOL_MAX_SIZE` | `1` / `10` | 连接池大小 |
| `DB_CONNECT_RETRIES` | `30` | 启动时等待数据库的重试次数（每秒一次） |

本地开发（不用 Docker）：

```bash
pip install -r requirements.txt
export DATABASE_URL=postgresql://postgres:postgres@localhost:5432/calibration
psql "$DATABASE_URL" -f db/init.sql   # 初始化建表
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## API 一览

统一错误格式：`{"error": {"code": "...", "message": "..."}}`

### 证书

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/v1/certificates` | 登记证书（**要求 `Idempotency-Key` 请求头**），成功 201；幂等重放 200（响应头 `Idempotency-Replayed: true`） |
| `GET` | `/api/v1/certificates?device_id=&status=&limit=&offset=` | 列表（`status` 取 `active`/`revoked`） |
| `GET` | `/api/v1/certificates/{id}` | 详情 |
| `POST` | `/api/v1/certificates/{id}/revoke` | 撤销，请求体 `{"reason": "..."}` |

登记请求体：

```json
{
  "device_id": "EQ-001",
  "certificate_no": "CERT-2026-0001",
  "valid_from": "2026-01-01T00:00:00+08:00",
  "valid_to": "2027-01-01T00:00:00+08:00"
}
```

典型错误：

| 状态码 | code | 场景 |
|---|---|---|
| 422 | （校验错误） | 时刻缺时区、`valid_from >= valid_to` |
| 409 | `VALIDITY_OVERLAP` | 同设备有效期重叠（含并发冲突） |
| 409 | `DUPLICATE_CERTIFICATE_NO` | 同设备证书编号重复 |
| 409 | `IDEMPOTENCY_KEY_CONFLICT` | 同一幂等键提交了不同内容 |
| 409 | `CERTIFICATE_ALREADY_REVOKED` | 重复撤销 |

### 测量记录

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/v1/measurements` | 写入测量记录，成功 201 |
| `POST` | `/api/v1/measurement-batches` | 批量写入（批次键 + 有序记录），成功 201；幂等重放 200（响应头 `Idempotency-Replayed: true`） |
| `GET` | `/api/v1/measurements?device_id=&limit=&offset=` | 列表，含实时有效性 |
| `GET` | `/api/v1/measurements/{id}` | 详情（原始登记版本），含实时有效性 |
| `POST` | `/api/v1/measurements/{id}/corrections` | 提交更正（**须携带当前版本号**），成功 201 返回新版本 |
| `GET` | `/api/v1/measurements/{id}/history` | 完整历史：原始版本 + 按序更正链 + 当前版本 |

写入请求体：

```json
{
  "device_id": "EQ-001",
  "measured_at": "2026-06-15T10:30:00+08:00",
  "certificate_no": "CERT-2026-0001"
}
```

仅当 `valid_from <= measured_at < valid_to` 且证书未撤销时接受，否则 422
（`CERTIFICATE_NOT_FOUND` / `CERTIFICATE_REVOKED` / `MEASUREMENT_TIME_NOT_COVERED`）。

响应中的 `validity` 按**当前**证书状态实时计算——证书撤销后，历史记录立即变为无效并给出原因：

```json
{
  "id": 1,
  "device_id": "EQ-001",
  "measured_at": "2026-06-15T02:30:00+00:00",
  "certificate_no": "CERT-2026-0001",
  "certificate_id": 1,
  "created_at": "2026-10-05T12:00:00+00:00",
  "validity": {
    "is_valid": false,
    "reason": "certificate revoked at 2026-10-05T13:00:00+00:00: calibration lab found drift"
  }
}
```

### 测量批次

实验室一次上传一批记录时使用。请求体含**批次键** `batch_key` 与**按顺序排列**的 `records`
（每条字段与单条写入相同，时刻必须带时区；1–1000 条）：

```json
{
  "batch_key": "lab-upload-2026-10-06-01",
  "records": [
    {"device_id": "EQ-001", "measured_at": "2026-06-15T10:30:00+08:00", "certificate_no": "CERT-2026-0001"},
    {"device_id": "EQ-001", "measured_at": "2026-06-16T10:30:00+08:00", "certificate_no": "CERT-2026-0001"},
    {"device_id": "EQ-001", "measured_at": "2027-02-01T09:00:00+08:00", "certificate_no": "CERT-2027-0001"}
  ]
}
```

语义：

- **逐条沿用单条写入校验**：证书须属于该设备、未撤销，且左闭右开 `[valid_from, valid_to)` 覆盖测量时刻。
  **任一条不合格时返回该条序号（`details.index`，从 1 开始）与原因，整批回滚、一条都不落库**。
- 整批成功后 201 返回 `record_ids`——**与输入顺序一一对应**的记录标识（同时附按序展开的 `records`）；
  批次记录与单条记录同表，同样**一经写入不可修改**，可经既有查询/更正接口访问。
- **幂等重试**：相同 `batch_key` + 相同**有序内容**（`measured_at` 统一换算 **UTC** 后比较，
  故同一时刻换时区写法仍视为同内容；记录顺序不同视为异内容）重试 → 200 返回**首次结果快照**
  （响应头 `Idempotency-Replayed: true`），快照不随后续证书撤销而改变。
- 相同键 + 不同内容 → 409 `IDEMPOTENCY_KEY_CONFLICT`；校验失败的批次**不占用**批次键，修正内容后可重试。
- **并发同一键至多生成一批**：事务级咨询锁按批次键串行化，唯一约束兜底；输家自动重放赢家结果或收 409。
- **与证书撤销互斥**：批次事务在校验前一次性对本批涉及的全部证书行按 id 排序加 `FOR UPDATE`
  （排序避免多批间死锁），撤销必须等待批次提交；批次也不会在锁等待结束后读到已撤销证书而写入。

成功响应：

```json
{
  "batch_key": "lab-upload-2026-10-06-01",
  "record_count": 3,
  "record_ids": [3, 4, 5],
  "records": [{"id": 3, "...": "..."}, {"id": 4, "...": "..."}, {"id": 5, "...": "..."}],
  "created_at": "2026-10-06T11:50:34.958379+00:00"
}
```

典型错误（HTTP 422，错误体内带失败序号）：

```json
{"error": {"code": "MEASUREMENT_TIME_NOT_COVERED",
           "message": "record at index 2 rejected: certificate covers [...] but measurement time is ...",
           "details": {"index": 2}}}
```

| 状态码 | code | 场景 |
|---|---|---|
| 422 | `CERTIFICATE_NOT_FOUND` / `CERTIFICATE_REVOKED` / `MEASUREMENT_TIME_NOT_COVERED` | 第 `details.index` 条不合格，整批未落库 |
| 422 | （校验错误） | 时刻缺时区、`records` 为空、缺 `batch_key` |
| 409 | `IDEMPOTENCY_KEY_CONFLICT` | 同一批次键提交了不同的有序内容 |
| 409 | `IDEMPOTENCY_IN_FLIGHT` | 同键并发且赢家尚未提交（稍后重试即可） |

### 测量更正

发现测量时刻或所用证书填错时，可对指定记录提交**带原因的更正**。原始登记与历次更正**只追加保存，任何版本不得修改或删除**（数据库触发器强制）。

- 原始登记为 **version 1**，每次更正追加一个版本（+1）；更正须携带 `base_version`（当前最新版本号），**版本不符返回 409 `VERSION_CONFLICT`**；同一版本的并发更正由行锁 + 唯一约束保证**最多成功一次**
- **设备编号不可更正**，始终沿用原始记录的值；`measured_at`（须带时区）与 `certificate_no` 至少提供一个，缺省字段沿用当前版本值
- 所选证书必须**属于该设备、覆盖新时刻且提交时未撤销**；校验与写入在同一事务内并对证书行加 `FOR UPDATE` 锁，并发撤销不会造成无效更正

更正请求体：

```json
{
  "base_version": 1,
  "measured_at": "2027-03-10T09:00:00+08:00",
  "certificate_no": "CERT-2027-0001",
  "reason": "recorded against wrong certificate; re-associated per lab audit"
}
```

成功 201 返回新版本（`version`、`measured_at`、`certificate_no`、`reason`、`validity` 等）。典型错误：

| 状态码 | code | 场景 |
|---|---|---|
| 404 | `MEASUREMENT_NOT_FOUND` | 测量记录不存在 |
| 409 | `VERSION_CONFLICT` | `base_version` 与当前最新版本不符（含并发更正冲突） |
| 422 | （校验错误） | 新时刻缺时区、两个更正字段都未提供 |
| 422 | `CERTIFICATE_NOT_FOUND` | 证书不存在或不属于该设备 |
| 422 | `CERTIFICATE_REVOKED` | 目标证书已撤销 |
| 422 | `MEASUREMENT_TIME_NOT_COVERED` | 证书有效期未覆盖新时刻 |

历史查询 `GET /api/v1/measurements/{id}/history` 一次返回**原始版本、按版本号升序的更正链和当前版本**；每个版本的有效性均按证书撤销状态**实时重算**，旧版历史保留可查：

```json
{
  "measurement_id": 1,
  "device_id": "EQ-001",
  "original":    {"version": 1, "measured_at": "2026-06-15T02:30:00+00:00", "certificate_no": "CERT-2026-0001", "reason": null, "validity": {"is_valid": true, "reason": "..."}, "...": "..."},
  "corrections": [{"version": 2, "measured_at": "2027-03-10T01:00:00+00:00", "certificate_no": "CERT-2027-0001", "reason": "recorded against wrong certificate; ...", "validity": {"is_valid": true, "reason": "..."}, "...": "..."}],
  "current":     {"version": 2, "...": "..."}
}
```

## 调用示例

完整端到端示例（登记 → 幂等重试 → 冲突 → 测量 → 撤销 → 有效性查询 → 更正 → 版本冲突/并发 → 历史查询 → 批次写入/幂等/原子性/撤销重放）：

```bash
./examples/demo.sh
```

最小示例：

```bash
# 登记证书
curl -X POST http://localhost:8000/api/v1/certificates \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: reg-eq001-0001' \
  -d '{"device_id":"EQ-001","certificate_no":"CERT-2026-0001",
       "valid_from":"2026-01-01T00:00:00+08:00","valid_to":"2027-01-01T00:00:00+08:00"}'

# 写入测量记录
curl -X POST http://localhost:8000/api/v1/measurements \
  -H 'Content-Type: application/json' \
  -d '{"device_id":"EQ-001","measured_at":"2026-06-15T10:30:00+08:00",
       "certificate_no":"CERT-2026-0001"}'

# 批量写入测量记录（批次键 + 有序记录；同键同内容重试返回首次结果）
curl -X POST http://localhost:8000/api/v1/measurement-batches \
  -H 'Content-Type: application/json' \
  -d '{"batch_key":"lab-upload-2026-10-06-01","records":[
       {"device_id":"EQ-001","measured_at":"2026-06-15T10:30:00+08:00","certificate_no":"CERT-2026-0001"},
       {"device_id":"EQ-001","measured_at":"2026-06-16T10:30:00+08:00","certificate_no":"CERT-2026-0001"}]}'

# 撤销证书
curl -X POST http://localhost:8000/api/v1/certificates/1/revoke \
  -H 'Content-Type: application/json' \
  -d '{"reason":"calibration lab found drift"}'

# 查询测量记录当前有效性
curl http://localhost:8000/api/v1/measurements?device_id=EQ-001

# 更正测量记录 1：换证书并调整时刻（base_version 为当前最新版本号，原始登记为 1）
curl -X POST http://localhost:8000/api/v1/measurements/1/corrections \
  -H 'Content-Type: application/json' \
  -d '{"base_version":1,"measured_at":"2027-03-10T09:00:00+08:00",
       "certificate_no":"CERT-2027-0001","reason":"recorded against wrong certificate"}'

# 查询完整历史：原始版本 + 更正链 + 当前版本
curl http://localhost:8000/api/v1/measurements/1/history
```

## 设计要点

- **区间语义**：有效期为 `[valid_from, valid_to)` 左闭右开，用 PostgreSQL `tstzrange(..., '[)')` 表达；端点相接的两段区间不算重叠。
- **并发不重叠**：`EXCLUDE USING gist (device_id WITH =, tstzrange WITH &&)` 约束在数据库层串行化并发写入，重叠提交只有一份成功，其余收到 409。
- **幂等**：`idempotency_key` 唯一约束 + 请求内容规范化哈希（时刻统一换算 UTC 后计算）；同键同内容返回首次结果，同键异内容 409。
- **测量写入一致性**：校验与写入在同一事务内，并对证书行加 `FOR UPDATE` 锁，防止校验后、写入前证书被并发撤销。
- **批次原子性**：批次在单事务内完成"锁证书 → 逐条校验 → 逐条插入"，任一记录不合格即整体回滚；涉及的证书行按 id 排序一次性 `FOR UPDATE` 锁定（多批并发无锁序死锁），与撤销互斥，不会通过校验却写入已撤销证书。
- **批次幂等**：`measurement_batches.batch_key` 唯一约束 + 事务级咨询锁 `pg_advisory_xact_lock(hashtext(batch_key))` 串行化同键并发（`SELECT ... FOR UPDATE` 在 READ COMMITTED 下不等待并发新插入的行，咨询锁补足这一窗口）；内容规范化哈希把测量时刻换算 UTC 后按序计算；首次成功的完整响应以 JSONB 快照保存，重放直接返回快照、不再校验证书状态，故证书撤销后同键重试结果不变。
- **批次行终态**：事务内先取批次序列 id、插入测量记录，最后一次性插入批次行（外键 `DEFERRABLE INITIALLY DEFERRED` 在提交时检查），快照写入即终态；批次表挂不可变触发器。
- **不可篡改**：测量表、批次表与更正表均挂 `BEFORE UPDATE OR DELETE` 触发器直接报错，应用层也不提供修改/删除接口。
- **追加式更正链**：更正写入 `measurement_corrections` 表，原始登记为 version 1、每次更正 +1；`(measurement_id, version)` 唯一约束 + 测量记录行锁串行化并发更正，同一版本的并发更正最多成功一次，其余收到 409 `VERSION_CONFLICT`。
- **更正校验一致性**：更正与撤销互斥——更正事务对目标证书行加 `FOR UPDATE` 锁后再校验撤销状态与有效期覆盖，并发撤销要么先完成（更正看到已撤销并拒绝）、要么等待更正提交，不会产生基于已撤销证书的无效更正。
- **撤销即生效**：测量查询实时 JOIN 证书状态，撤销立即反映到全部历史记录（含各更正版本）的有效性上，历史数据本身保留不动。

## 目录结构

```
├── app/
│   ├── main.py            # FastAPI 入口、生命周期、路由挂载
│   ├── config.py          # 环境变量配置
│   ├── db.py              # asyncpg 连接池
│   ├── errors.py          # 统一错误格式
│   ├── schemas.py         # 请求/响应模型（AwareDatetime 强制时区）
│   └── routers/
│       ├── certificates.py
│       ├── measurements.py
│       └── batches.py       # 测量批次写入（整批校验/落库、批次键幂等）
├── db/init.sql            # 建表、约束、不可变触发器（容器首次启动自动执行）
├── examples/demo.sh       # 端到端调用示例
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```
