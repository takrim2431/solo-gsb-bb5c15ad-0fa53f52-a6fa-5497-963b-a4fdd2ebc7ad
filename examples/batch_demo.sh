#!/usr/bin/env bash
# 测量批次写入端到端示例：
#   成功批次 -> 同键同内容重放（时刻换等价时区）-> 同键异内容冲突 ->
#   逐条校验失败（返回序号、整批不落库）-> 失败结果重放 ->
#   撤销证书后重放仍返回首次结果 -> 并发同键至多一批 -> 与撤销并发的安全性。
# 用法: BASE=http://localhost:8000 ./examples/batch_demo.sh
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"
API="$BASE/api/v1"

if command -v jq >/dev/null 2>&1; then PPRINT="jq ."; else PPRINT="cat"; fi

post() { # post <path> <json-body> [extra curl args]
  local path="$1" body="$2"; shift 2
  echo "==> POST $path"
  curl -sS -X POST "$API$path" -H 'Content-Type: application/json' "$@" -d "$body" | eval "$PPRINT"
  echo
}

echo "### 0. 健康检查"
curl -sS "$BASE/health" | eval "$PPRINT"; echo

echo "### 1. 登记两张有效证书"
post /certificates \
  '{"device_id":"EQ-B1","certificate_no":"CERT-2026-B1","valid_from":"2026-01-01T00:00:00+08:00","valid_to":"2027-01-01T00:00:00+08:00"}' \
  -H 'Idempotency-Key: batch-demo-cert-1'
post /certificates \
  '{"device_id":"EQ-B2","certificate_no":"CERT-2026-B2","valid_from":"2026-01-01T00:00:00+00:00","valid_to":"2027-01-01T00:00:00+00:00"}' \
  -H 'Idempotency-Key: batch-demo-cert-2'

echo "### 2. 批次写入两条记录 → 201，measurement_ids 与输入顺序一一对应"
post /measurement-batches \
  '{"batch_key":"lab-upload-20261006-001","records":[
      {"device_id":"EQ-B1","measured_at":"2026-06-15T10:30:00+08:00","certificate_no":"CERT-2026-B1"},
      {"device_id":"EQ-B2","measured_at":"2026-06-15T02:30:00+00:00","certificate_no":"CERT-2026-B2"}]}'

echo "### 3. 同键同内容重试（第 1 条换等价时区写法，UTC 时刻相同）→ 200 返回首次 id，响应头 Batch-Replayed: true"
curl -sS -i -X POST "$API/measurement-batches" -H 'Content-Type: application/json' \
  -d '{"batch_key":"lab-upload-20261006-001","records":[
      {"device_id":"EQ-B1","measured_at":"2026-06-15T11:30:00+09:00","certificate_no":"CERT-2026-B1"},
      {"device_id":"EQ-B2","measured_at":"2026-06-15T10:30:00+08:00","certificate_no":"CERT-2026-B2"}]}' | head -20
echo

echo "### 4. 同键不同内容（调换顺序）→ 409 BATCH_KEY_CONFLICT"
post /measurement-batches \
  '{"batch_key":"lab-upload-20261006-001","records":[
      {"device_id":"EQ-B2","measured_at":"2026-06-15T02:30:00+00:00","certificate_no":"CERT-2026-B2"},
      {"device_id":"EQ-B1","measured_at":"2026-06-15T10:30:00+08:00","certificate_no":"CERT-2026-B1"}]}'

echo "### 5. 逐条校验：第 2 条（序号 1）证书不属于该设备 → 422，返回 index=1 与原因，整批不落库"
post /measurement-batches \
  '{"batch_key":"lab-upload-20261006-002","records":[
      {"device_id":"EQ-B1","measured_at":"2026-06-16T10:00:00+08:00","certificate_no":"CERT-2026-B1"},
      {"device_id":"EQ-B1","measured_at":"2026-06-16T10:00:00+08:00","certificate_no":"CERT-2026-B2"}]}'

echo "### 6. 失败批次同键同内容重试 → 原样重放首次失败结果（422 + index=1）"
post /measurement-batches \
  '{"batch_key":"lab-upload-20261006-002","records":[
      {"device_id":"EQ-B1","measured_at":"2026-06-16T10:00:00+08:00","certificate_no":"CERT-2026-B1"},
      {"device_id":"EQ-B1","measured_at":"2026-06-16T10:00:00+08:00","certificate_no":"CERT-2026-B2"}]}'

echo "### 7. 撤销第 1 张证书后，重放步骤 2 的成功批次 → 仍返回首次结果 200（不重新校验、不写入新记录）"
CID=$(curl -sS "$API/certificates?device_id=EQ-B1" | python3 -c 'import sys,json;print(json.load(sys.stdin)[0]["id"])')
post "/certificates/$CID/revoke" '{"reason":"batch replay demo revoke"}'
post /measurement-batches \
  '{"batch_key":"lab-upload-20261006-001","records":[
      {"device_id":"EQ-B1","measured_at":"2026-06-15T10:30:00+08:00","certificate_no":"CERT-2026-B1"},
      {"device_id":"EQ-B2","measured_at":"2026-06-15T02:30:00+00:00","certificate_no":"CERT-2026-B2"}]}'

echo "### 8. 撤销后用新批次引用已撤销证书 → 422 CERTIFICATE_REVOKED index=0"
post /measurement-batches \
  '{"batch_key":"lab-upload-20261006-003","records":[
      {"device_id":"EQ-B1","measured_at":"2026-07-01T09:00:00+08:00","certificate_no":"CERT-2026-B1"}]}'

echo "### 9. 并发提交同一键同内容：恰好一批落库，其余 200 重放（或 409 BATCH_KEY_IN_FLIGHT，重试即可）"
post /certificates \
  '{"device_id":"EQ-B3","certificate_no":"CERT-2026-B3","valid_from":"2026-01-01T00:00:00+08:00","valid_to":"2028-01-01T00:00:00+08:00"}' \
  -H 'Idempotency-Key: batch-demo-cert-3'
body='{"batch_key":"lab-upload-concurrent-001","records":[{"device_id":"EQ-B3","measured_at":"2026-05-01T08:00:00+08:00","certificate_no":"CERT-2026-B3"}]}'
for i in 1 2 3 4; do
  curl -sS -o /tmp/bc$i.json -w "req $i -> HTTP %{http_code}\n" \
    -X POST "$API/measurement-batches" -H 'Content-Type: application/json' -d "$body" &
done
wait
for i in 1 2 3 4; do echo "  resp $i: $(cat /tmp/bc$i.json)"; done

echo "### 10. 批次写入的记录可用既有查询接口按 id 查询（实时有效性）"
FIRST_ID=$(curl -sS -X POST "$API/measurement-batches" -H 'Content-Type: application/json' \
  -d '{"batch_key":"lab-upload-query-001","records":[{"device_id":"EQ-B2","measured_at":"2026-08-01T09:00:00+08:00","certificate_no":"CERT-2026-B2"}]}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["measurement_ids"][0])')
echo "==> GET /measurements/$FIRST_ID"
curl -sS "$API/measurements/$FIRST_ID" | eval "$PPRINT"; echo
