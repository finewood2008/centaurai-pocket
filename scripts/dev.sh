#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
api_pid=""

cleanup() {
  if [[ -n "${api_pid}" ]] && kill -0 "${api_pid}" >/dev/null 2>&1; then
    kill "${api_pid}" >/dev/null 2>&1 || true
    wait "${api_pid}" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

(
  cd "${project_root}/services/api"
  uv run centaur-pocket-api
) &
api_pid=$!

api_ready=0
for _attempt in $(seq 1 50); do
  if curl --silent --fail \
    "http://127.0.0.1:8718/api/v1/health" >/dev/null 2>&1; then
    api_ready=1
    break
  fi
  sleep 0.1
done

if [[ "${api_ready}" -ne 1 ]]; then
  echo "Pocket API 未能在 8718 端口就绪。" >&2
  exit 1
fi

echo "Pocket API 已启动：http://127.0.0.1:8718"
echo "正在启动 Expo Web；按 Ctrl+C 同时停止两个服务。"
(
  cd "${project_root}/apps/mobile"
  npm run web
)
