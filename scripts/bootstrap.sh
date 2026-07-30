#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

for required_command in uv npm; do
  if ! command -v "${required_command}" >/dev/null 2>&1; then
    echo "缺少命令：${required_command}" >&2
    exit 1
  fi
done

echo "安装 Pocket API 依赖…"
(
  cd "${project_root}/services/api"
  uv sync --group dev
)

echo "安装 Pocket Mobile 依赖…"
(
  cd "${project_root}/apps/mobile"
  if [[ -f package-lock.json ]]; then
    npm ci
  else
    npm install
  fi
)

echo "CentaurAI Pocket 依赖安装完成。"
