#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "验证 Pocket API…"
(
  cd "${project_root}/services/api"
  uv run pytest -q
  uvx ruff check src tests
)

echo "验证 Pocket Mobile…"
(
  cd "${project_root}/apps/mobile"
  npm run typecheck
  npm run lint
  npm test
  npx expo-doctor
  npm run export:web
)

if [[ -d "${project_root}/apps/desktop/node_modules" ]]; then
  echo "验证 Pocket Desktop…"
  (
    cd "${project_root}/apps/desktop"
    npm run check
    npm test
  )
else
  echo "Pocket Desktop 依赖尚未安装；运行 make desktop 会完成安装与验证。"
fi

echo "CentaurAI Pocket 全部验证通过。"
