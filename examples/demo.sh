#!/usr/bin/env bash
# 端到端调用示例：覆盖证书登记、幂等、区间冲突、测量校验、撤销与实时有效性，
# 测量更正（版本冲突、并发唯一成功、完整历史查询），以及测量批次写入
# （整批校验、批次键幂等、撤销后重放、整批原子性）。
# 用法: BASE=http://localhost:8000 ./examples/demo.sh
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"
API="$BASE/api/v1"

if command -v jq >/dev/null 2>&1; then PPRINT="jq ."; else PPRINT="cat"; fi

post() { # post <path> <json-body> [extra curl args, e.g. -H 'Idempotency-Key: ...']
  local path="$1" body="$2"; shift 2
  echo "==> POST $path"
  curl -sS -X POST "$API$path" -H 'Content-Type: application/json' "$@" -d "$body" | eval "$PPRINT"
  echo
}

get() { # get <path>
  echo "==> GET $1"
  curl -sS "$API$1" | eval "$PPRINT"
  echo
}

echo "### 0. 健康检查"
curl -sS "$BASE/health" | eval "$PPRINT"; echo

echo "### 1. 登记证书（201）"
post /certificates \
  '{"device_id":"EQ-001","certificate_no":"CERT-2026-0001","valid_from":"2026-01-01T00:00:00+08:00","valid_to":"2027-01-01T00:00:00+08:00"}' \
  -H 'Idempotency-Key: reg-eq001-0001'

echo "### 2. 相同幂等键 + 相同内容重试 → 200 返回首次结果（响应头含 Idempotency-Replayed: true）"
curl -sS -i -X POST "$API/certificates" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: reg-eq001-0001' \
  -d '{"device_id":"EQ-001","certificate_no":"CERT-2026-0001","valid_from":"2026-01-01T00:00:00+08:00","valid_to":"2027-01-01T00:00:00+08:00"}' | head -20
echo

echo "### 3. 相同幂等键 + 不同内容 → 409 IDEMPOTENCY_KEY_CONFLICT"
post /certificates \
  '{"device_id":"EQ-001","certificate_no":"CERT-2026-0001","valid_from":"2026-02-01T00:00:00+08:00","valid_to":"2027-02-01T00:00:00+08:00"}' \
  -H 'Idempotency-Key: reg-eq001-0001'

echo "### 4. 同设备有效期重叠 → 409 VALIDITY_OVERLAP（并发提交同样只有一份成功）"
post /certificates \
  '{"device_id":"EQ-001","certificate_no":"CERT-2026-0002","valid_from":"2026-06-01T00:00:00+08:00","valid_to":"2027-06-01T00:00:00+08:00"}' \
  -H 'Idempotency-Key: reg-eq001-0002'

echo "### 5. 时刻缺时区 → 422；起点不早于终点 → 422"
post /certificates \
  '{"device_id":"EQ-002","certificate_no":"CERT-X","valid_from":"2026-01-01T00:00:00","valid_to":"2027-01-01T00:00:00+08:00"}' \
  -H 'Idempotency-Key: reg-bad-1'
post /certificates \
  '{"device_id":"EQ-002","certificate_no":"CERT-X","valid_from":"2027-01-01T00:00:00+08:00","valid_to":"2026-01-01T00:00:00+08:00"}' \
  -H 'Idempotency-Key: reg-bad-2'

echo "### 6. 紧邻上一段的新证书（左闭右开，端点相接不算重叠）→ 201"
post /certificates \
  '{"device_id":"EQ-001","certificate_no":"CERT-2027-0001","valid_from":"2027-01-01T00:00:00+08:00","valid_to":"2028-01-01T00:00:00+08:00"}' \
  -H 'Idempotency-Key: reg-eq001-2027'

echo "### 7. 写入测量记录：落在有效期内 → 201"
post /measurements \
  '{"device_id":"EQ-001","measured_at":"2026-06-15T10:30:00+08:00","certificate_no":"CERT-2026-0001"}'

echo "### 8. 边界：measured_at == valid_from（左闭）→ 201；== valid_to（右开）→ 422"
post /measurements \
  '{"device_id":"EQ-001","measured_at":"2026-01-01T00:00:00+08:00","certificate_no":"CERT-2026-0001"}'
post /measurements \
  '{"device_id":"EQ-001","measured_at":"2027-01-01T00:00:00+08:00","certificate_no":"CERT-2026-0001"}'

echo "### 9. 证书不存在 → 422"
post /measurements \
  '{"device_id":"EQ-001","measured_at":"2026-06-15T10:30:00+08:00","certificate_no":"NO-SUCH-CERT"}'

echo "### 10. 撤销证书（历史测量保留，不删除）"
post /certificates/1/revoke '{"reason":"calibration lab found drift, cert voided"}'

echo "### 11. 撤销后写入新测量 → 422 CERTIFICATE_REVOKED"
post /measurements \
  '{"device_id":"EQ-001","measured_at":"2026-07-01T09:00:00+08:00","certificate_no":"CERT-2026-0001"}'

echo "### 12. 查询测量记录：实时给出当前有效性及原因（历史记录变为 invalid）"
get "/measurements?device_id=EQ-001"

echo "### 13. 重复撤销 → 409 CERTIFICATE_ALREADY_REVOKED"
post /certificates/1/revoke '{"reason":"again"}'

echo "### 14. 更正测量记录 1：换用有效证书并调整时刻（携带当前版本号 base_version=1）→ 201，version=2"
post /measurements/1/corrections \
  '{"base_version":1,"measured_at":"2027-03-10T09:00:00+08:00","certificate_no":"CERT-2027-0001","reason":"recorded against wrong certificate; re-associated per lab audit"}'

echo "### 15. 用旧版本号再次更正 → 409 VERSION_CONFLICT（只能从最新版本继续）"
post /measurements/1/corrections \
  '{"base_version":1,"measured_at":"2027-03-11T09:00:00+08:00","reason":"stale version"}'

echo "### 16. 基于最新版本继续更正：只改时刻、证书沿用（设备编号始终不可改）→ 201，version=3"
post /measurements/1/corrections \
  '{"base_version":2,"measured_at":"2027-03-12T09:00:00+08:00","reason":"clock drift confirmed by lab"}'

echo "### 17. 同一版本并发更正：最多成功一次（一个 201，其余 409）"
curl -sS -o /tmp/corr-a.json -w 'req A -> %{http_code}\n' -X POST "$API/measurements/1/corrections" \
  -H 'Content-Type: application/json' \
  -d '{"base_version":3,"measured_at":"2027-03-13T09:00:00+08:00","reason":"concurrent correction A"}' &
curl -sS -o /tmp/corr-b.json -w 'req B -> %{http_code}\n' -X POST "$API/measurements/1/corrections" \
  -H 'Content-Type: application/json' \
  -d '{"base_version":3,"measured_at":"2027-03-14T09:00:00+08:00","reason":"concurrent correction B"}' &
wait
eval "$PPRINT" < /tmp/corr-a.json; eval "$PPRINT" < /tmp/corr-b.json

echo "### 18. 更正到已撤销的证书 → 422 CERTIFICATE_REVOKED（校验与写入同事务加锁，并发撤销不会造成无效更正）"
post /measurements/1/corrections \
  '{"base_version":4,"certificate_no":"CERT-2026-0001","measured_at":"2026-06-15T10:30:00+08:00","reason":"try to switch back to revoked cert"}'

echo "### 19. 查询完整历史：原始版本 + 按序更正链 + 当前版本（各版本有效性按证书撤销状态实时重算，旧版历史保留可查）"
get /measurements/1/history

echo "### 20. 批次写入：为批次演示登记两份不重叠的新证书（EQ-100）→ 201"
post /certificates \
  '{"device_id":"EQ-100","certificate_no":"CERT-2026-1001","valid_from":"2026-01-01T00:00:00+08:00","valid_to":"2027-01-01T00:00:00+08:00"}' \
  -H 'Idempotency-Key: reg-eq100-2026'
post /certificates \
  '{"device_id":"EQ-100","certificate_no":"CERT-2027-1001","valid_from":"2027-01-01T00:00:00+08:00","valid_to":"2028-01-01T00:00:00+08:00"}' \
  -H 'Idempotency-Key: reg-eq100-2027'

echo "### 21. 整批提交 3 条有序记录 → 201，record_ids 与输入顺序一一对应"
post /measurement-batches \
  '{"batch_key":"lab-upload-2026-10-06-01","records":[
    {"device_id":"EQ-100","measured_at":"2026-06-15T10:30:00+08:00","certificate_no":"CERT-2026-1001"},
    {"device_id":"EQ-100","measured_at":"2026-06-16T10:30:00+08:00","certificate_no":"CERT-2026-1001"},
    {"device_id":"EQ-100","measured_at":"2027-02-01T09:00:00+08:00","certificate_no":"CERT-2027-1001"}
  ]}'

echo "### 22. 相同批次键 + 相同有序内容重试 → 200 返回首次结果（响应头 Idempotency-Replayed: true）"
curl -sS -i -X POST "$API/measurement-batches" -H 'Content-Type: application/json' \
  -d '{"batch_key":"lab-upload-2026-10-06-01","records":[
    {"device_id":"EQ-100","measured_at":"2026-06-15T10:30:00+08:00","certificate_no":"CERT-2026-1001"},
    {"device_id":"EQ-100","measured_at":"2026-06-16T10:30:00+08:00","certificate_no":"CERT-2026-1001"},
    {"device_id":"EQ-100","measured_at":"2027-02-01T09:00:00+08:00","certificate_no":"CERT-2027-1001"}
  ]}' | head -12
echo

echo "### 22b. 时刻换时区写法（同一 UTC 时刻）仍视为相同内容 → 200 重放"
post /measurement-batches \
  '{"batch_key":"lab-upload-2026-10-06-01","records":[
    {"device_id":"EQ-100","measured_at":"2026-06-15T11:30:00+09:00","certificate_no":"CERT-2026-1001"},
    {"device_id":"EQ-100","measured_at":"2026-06-16T03:30:00+01:00","certificate_no":"CERT-2026-1001"},
    {"device_id":"EQ-100","measured_at":"2027-02-01T01:00:00+00:00","certificate_no":"CERT-2027-1001"}
  ]}'

echo "### 23. 相同批次键 + 不同内容（含顺序调换）→ 409 IDEMPOTENCY_KEY_CONFLICT"
post /measurement-batches \
  '{"batch_key":"lab-upload-2026-10-06-01","records":[
    {"device_id":"EQ-100","measured_at":"2026-06-16T10:30:00+08:00","certificate_no":"CERT-2026-1001"},
    {"device_id":"EQ-100","measured_at":"2026-06-15T10:30:00+08:00","certificate_no":"CERT-2026-1001"},
    {"device_id":"EQ-100","measured_at":"2027-02-01T09:00:00+08:00","certificate_no":"CERT-2027-1001"}
  ]}'

echo "### 24. 第 2 条落在证书有效期外 → 422，details.index=2，且整批不落库"
post /measurement-batches \
  '{"batch_key":"lab-upload-2026-10-06-bad","records":[
    {"device_id":"EQ-100","measured_at":"2026-06-15T10:30:00+08:00","certificate_no":"CERT-2026-1001"},
    {"device_id":"EQ-100","measured_at":"2028-06-15T10:30:00+08:00","certificate_no":"CERT-2027-1001"}
  ]}'

echo "### 25. 失败批次不占用批次键：内容修正后用同键重试 → 201"
post /measurement-batches \
  '{"batch_key":"lab-upload-2026-10-06-bad","records":[
    {"device_id":"EQ-100","measured_at":"2026-06-15T10:30:00+08:00","certificate_no":"CERT-2026-1001"}
  ]}'

echo "### 26. 撤销 CERT-2026-1001，然后重放批次 21 → 200 且快照仍 is_valid=true（首次结果不因撤销改变）"
CERT_ID=$(curl -sS "$API/certificates?device_id=EQ-100" | python3 -c '
import json,sys
for c in json.load(sys.stdin):
    if c["certificate_no"] == "CERT-2026-1001": print(c["id"])')
post "/certificates/$CERT_ID/revoke" '{"reason":"drift found in October audit"}'
post /measurement-batches \
  '{"batch_key":"lab-upload-2026-10-06-01","records":[
    {"device_id":"EQ-100","measured_at":"2026-06-15T10:30:00+08:00","certificate_no":"CERT-2026-1001"},
    {"device_id":"EQ-100","measured_at":"2026-06-16T10:30:00+08:00","certificate_no":"CERT-2026-1001"},
    {"device_id":"EQ-100","measured_at":"2027-02-01T09:00:00+08:00","certificate_no":"CERT-2027-1001"}
  ]}'

echo "### 27. 撤销后用新批次键引用已撤销证书 → 422 CERTIFICATE_REVOKED, details.index=1，整批不落库"
post /measurement-batches \
  '{"batch_key":"lab-upload-after-revoke","records":[
    {"device_id":"EQ-100","measured_at":"2026-08-01T10:30:00+08:00","certificate_no":"CERT-2026-1001"}
  ]}'

echo "### 28. 批次记录可经既有查询接口读到（实时有效性反映撤销状态）"
get "/measurements?device_id=EQ-100&limit=20"
