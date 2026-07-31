#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
api_root="${project_root}/services/api"
mobile_root="${project_root}/apps/mobile"
desktop_root="${project_root}/apps/desktop"
backend_vendor_root="${desktop_root}/vendor/backend"

command -v uv >/dev/null 2>&1 || {
  echo "缺少 uv，无法构建桌面数据服务。" >&2
  exit 1
}
command -v npm >/dev/null 2>&1 || {
  echo "缺少 npm，无法构建 Electron 桌面应用。" >&2
  exit 1
}

echo "[1/4] 导出 CentaurAI Pocket Web 界面"
npm --prefix "${mobile_root}" run export:web

echo "[2/4] 构建独立 Python 数据服务"
mkdir -p "${backend_vendor_root}"
uv run \
  --project "${api_root}" \
  --with "pyinstaller>=6.16,<7" \
  pyinstaller \
  --clean \
  --noconfirm \
  --onedir \
  --name centaur-pocket-api \
  --paths "${api_root}/src" \
  --distpath "${backend_vendor_root}" \
  --workpath "${desktop_root}/.pyinstaller/build" \
  --specpath "${desktop_root}/.pyinstaller" \
  "${api_root}/desktop_entry.py"

echo "[3/4] 安装 Electron 构建依赖"
npm --prefix "${desktop_root}" install --no-audit --no-fund

echo "[4/4] 封装 Electron Linux 应用"
npm --prefix "${desktop_root}" run check
npm --prefix "${desktop_root}" test
npm --prefix "${desktop_root}" run pack:dir

package_root="${desktop_root}/release/linux-unpacked"
test -x "${package_root}/centaurai-pocket"
test -f "${package_root}/resources/app.asar"
test -f "${package_root}/resources/web/index.html"
test -f "${package_root}/resources/assets/icon.png"
test -x "${package_root}/resources/backend/centaur-pocket-api"

manifest_tmp="$(mktemp)"
(
  cd "${package_root}"
  find . -type f ! -name release-manifest.sha256 -print0 |
    LC_ALL=C sort -z |
    xargs -0 sha256sum >"${manifest_tmp}"
)
install -m 0644 "${manifest_tmp}" "${package_root}/release-manifest.sha256"
rm -f "${manifest_tmp}"

echo
echo "桌面应用已生成："
echo "${package_root}/centaurai-pocket"
