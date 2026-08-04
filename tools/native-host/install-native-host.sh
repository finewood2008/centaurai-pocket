#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
INSTALL_ROOT="${XDG_DATA_HOME:-${HOME}/.local/share}/centaurai-pocket/wechat-observer"
HOST_PATH="${INSTALL_ROOT}/native_host.py"
MANIFEST_DIR="${HOME}/.mozilla/native-messaging-hosts"
MANIFEST_PATH="${MANIFEST_DIR}/ai.centaur.pocket.wechat_observer.json"

SOURCE_ID=""
API_BASE="http://127.0.0.1:8718"
PAIRING_CODE_FILE=""

usage() {
  echo "用法: $0 [--source-id ID --pairing-code-file FILE] [--api-base URL]" >&2
  echo "不提供配对参数时，可安装后在 Firefox 扩展弹窗中输入。" >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source-id)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      SOURCE_ID="$2"
      shift 2
      ;;
    --api-base)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      API_BASE="$2"
      shift 2
      ;;
    --pairing-code-file)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      PAIRING_CODE_FILE="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage
      exit 2
      ;;
  esac
done

if [[ -n "${SOURCE_ID}" || -n "${PAIRING_CODE_FILE}" ]]; then
  if [[ -z "${SOURCE_ID}" || -z "${PAIRING_CODE_FILE}" ]]; then
    echo "--source-id 与 --pairing-code-file 必须同时提供。" >&2
    exit 2
  fi
fi

command -v python3 >/dev/null || { echo "需要 Python 3.11 或更高版本。" >&2; exit 1; }

install -d -m 0700 "${INSTALL_ROOT}" "${MANIFEST_DIR}"
install -m 0755 "${SCRIPT_DIR}/native_host.py" "${HOST_PATH}"

MANIFEST_TMP="$(mktemp "${MANIFEST_DIR}/.wechat-observer-manifest.XXXXXX")"
cleanup() {
  rm -f -- "${MANIFEST_TMP}"
}
trap cleanup EXIT
python3 - "${HOST_PATH}" "${MANIFEST_TMP}" <<'PY'
import json
import os
import sys

host_path, output_path = sys.argv[1:]
manifest = {
    "name": "ai.centaur.pocket.wechat_observer",
    "description": "CentaurAI Pocket WeChat visible DOM observer",
    "path": os.path.abspath(host_path),
    "type": "stdio",
    "allowed_extensions": ["centaur-pocket-wechat-observer@centaur.ai"],
}
with open(output_path, "w", encoding="utf-8") as handle:
    json.dump(manifest, handle, ensure_ascii=False, indent=2)
    handle.write("\n")
PY
chmod 0600 "${MANIFEST_TMP}"
mv -f -- "${MANIFEST_TMP}" "${MANIFEST_PATH}"
trap - EXIT

if [[ -n "${SOURCE_ID}" ]]; then
  "${HOST_PATH}" \
    --write-config \
    --source-id "${SOURCE_ID}" \
    --api-base "${API_BASE}" \
    --pairing-code-file "${PAIRING_CODE_FILE}"
fi

echo "Native Host 已安装到 ${HOST_PATH}"
echo "Firefox 用户级 manifest 已安装到 ${MANIFEST_PATH}"
if [[ -z "${SOURCE_ID}" ]]; then
  echo "下一步：加载扩展后，点击工具栏图标输入来源 ID 和一次性配对码。"
fi
