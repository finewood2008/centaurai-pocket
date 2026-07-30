#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
runtime_dir="$(mktemp -d)"
watched_dir="${runtime_dir}/watched"
api_log="${runtime_dir}/api.log"
api_pid=""
smoke_port="${CENTAURAI_POCKET_SMOKE_PORT:-18718}"
server_url="http://127.0.0.1:${smoke_port}"
owner_token="cp_owner_local-smoke"
agent_token="cp_live_local-smoke"

cleanup() {
  result=$?
  if [[ -n "${api_pid}" ]] && kill -0 "${api_pid}" >/dev/null 2>&1; then
    kill "${api_pid}" >/dev/null 2>&1 || true
    wait "${api_pid}" 2>/dev/null || true
  fi
  if [[ "${result}" -ne 0 && -f "${api_log}" ]]; then
    tail -80 "${api_log}" >&2 || true
  fi
  if [[ -d "${runtime_dir}" && "${runtime_dir}" == /tmp/* ]]; then
    rm -rf -- "${runtime_dir}"
  fi
}
trap cleanup EXIT INT TERM

for required_command in curl jq uv; do
  if ! command -v "${required_command}" >/dev/null 2>&1; then
    echo "缺少命令：${required_command}" >&2
    exit 1
  fi
done

if ! [[ "${smoke_port}" =~ ^[0-9]+$ ]]; then
  echo "CENTAURAI_POCKET_SMOKE_PORT 必须是端口号" >&2
  exit 1
fi

mkdir -p "${watched_dir}"
printf '%s\n' "SMOKECHECK governed private knowledge." \
  >"${watched_dir}/smoke-note.txt"

(
  cd "${project_root}/services/api"
  CENTAURAI_POCKET_DATA_DIR="${runtime_dir}/data" \
  CENTAURAI_POCKET_HOST="127.0.0.1" \
  CENTAURAI_POCKET_PORT="${smoke_port}" \
  CENTAURAI_POCKET_OWNER_TOKEN="${owner_token}" \
  CENTAURAI_POCKET_AGENT_TOKEN="${agent_token}" \
  CENTAURAI_POCKET_SCHEDULER_POLL_SECONDS="0" \
    uv run centaur-pocket-api >"${api_log}" 2>&1
) &
api_pid=$!

ready=0
for _attempt in $(seq 1 100); do
  if curl --silent --fail "${server_url}/api/v1/health" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 0.1
done
if [[ "${ready}" -ne 1 ]]; then
  echo "Pocket API 未能在 ${smoke_port} 端口就绪" >&2
  exit 1
fi

source_payload="$(
  jq -nc \
    --arg path "${watched_dir}" \
    '{
      kind: "folder",
      display_name: "冒烟测试目录",
      config: {path: $path, recursive: true, include_hidden: false},
      schedule: "manual",
      enabled: true
    }'
)"
source_response="$(
  curl --silent --show-error --fail-with-body \
    -X POST \
    -H "Authorization: Bearer ${owner_token}" \
    -H "Content-Type: application/json" \
    -H "Idempotency-Key: smoke-source-create" \
    -d "${source_payload}" \
    "${server_url}/api/v1/sources"
)"
source_id="$(jq -er '.id' <<<"${source_response}")"

sync_response="$(
  curl --silent --show-error --fail-with-body \
    -X POST \
    -H "Authorization: Bearer ${owner_token}" \
    -H "Idempotency-Key: smoke-source-sync" \
    "${server_url}/api/v1/sources/${source_id}/sync"
)"
jq -e '.status == "completed" and .imported_count == 1' \
  <<<"${sync_response}" >/dev/null

task_response="$(
  curl --silent --show-error --fail-with-body \
    -H "Authorization: Bearer ${owner_token}" \
    "${server_url}/api/v1/governance/tasks?status=pending&limit=10"
)"
task_id="$(jq -er '.items[0].id' <<<"${task_response}")"
curl --silent --show-error --fail-with-body \
  -X POST \
  -H "Authorization: Bearer ${owner_token}" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: smoke-governance-apply" \
  -d '{"patch":{"state":"ready"}}' \
  "${server_url}/api/v1/governance/tasks/${task_id}/apply" >/dev/null

agent_response="$(
  curl --silent --show-error --fail-with-body \
    -X POST \
    -H "Authorization: Bearer ${agent_token}" \
    -H "Content-Type: application/json" \
    -d '{"query":"SMOKECHECK","limit":5}' \
    "${server_url}/api/v1/agent/search"
)"
jq -e '.count == 1 and .visibility == "ready_only"' \
  <<<"${agent_response}" >/dev/null

capture_payload='{
  "title": "手机冒烟采集",
  "text": "MOBILESMOKECHECK private fragment.",
  "mimeType": "text/plain",
  "origin": "mobile-share"
}'
capture_response="$(
  curl --silent --show-error --fail-with-body \
    -X POST \
    -H "Authorization: Bearer ${owner_token}" \
    -H "Content-Type: application/json" \
    -H "Idempotency-Key: smoke-mobile-capture" \
    -d "${capture_payload}" \
    "${server_url}/api/v1/captures"
)"
capture_repeat="$(
  curl --silent --show-error --fail-with-body \
    -X POST \
    -H "Authorization: Bearer ${owner_token}" \
    -H "Content-Type: application/json" \
    -H "Idempotency-Key: smoke-mobile-capture" \
    -d "${capture_payload}" \
    "${server_url}/api/v1/captures"
)"
jq -e --arg repeated "$(jq -r '.item_id' <<<"${capture_repeat}")" \
  '.item_id == $repeated and .status == "needs_review"' \
  <<<"${capture_response}" >/dev/null

initialize_response="$(
  curl --silent --show-error --fail-with-body \
    -X POST \
    -H "Authorization: Bearer ${agent_token}" \
    -H "Content-Type: application/json" \
    -d '{
      "jsonrpc":"2.0",
      "id":1,
      "method":"initialize",
      "params":{
        "protocolVersion":"2025-06-18",
        "capabilities":{},
        "clientInfo":{"name":"pocket-smoke","version":"1.0"}
      }
    }' \
    "${server_url}/api/v1/mcp"
)"
jq -e '.result.protocolVersion == "2025-06-18"' \
  <<<"${initialize_response}" >/dev/null

mcp_response="$(
  curl --silent --show-error --fail-with-body \
    -X POST \
    -H "Authorization: Bearer ${agent_token}" \
    -H "MCP-Protocol-Version: 2025-06-18" \
    -H "Content-Type: application/json" \
    -d '{
      "jsonrpc":"2.0",
      "id":2,
      "method":"tools/call",
      "params":{
        "name":"knowledge_retrieve",
        "arguments":{"query":"SMOKECHECK","limit":5}
      }
    }' \
    "${server_url}/api/v1/mcp"
)"
jq -e '.result.structuredContent.count == 1' <<<"${mcp_response}" >/dev/null

echo "CentaurAI Pocket 真实 HTTP / MCP 冒烟测试通过。"
