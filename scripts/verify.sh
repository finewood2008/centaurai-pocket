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

echo "CentaurAI Pocket 全部验证通过。"
