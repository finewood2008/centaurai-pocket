#!/usr/bin/env bash
set -euo pipefail

INSTALL_ROOT="${XDG_DATA_HOME:-${HOME}/.local/share}/centaurai-pocket/wechat-observer"
HOST_PATH="${INSTALL_ROOT}/native_host.py"
MANIFEST_PATH="${HOME}/.mozilla/native-messaging-hosts/ai.centaur.pocket.wechat_observer.json"
CONFIG_PATH="${XDG_CONFIG_HOME:-${HOME}/.config}/centaurai-pocket/wechat-observer.json"
PURGE_CONFIG=0

if [[ ${1:-} == "--purge-config" ]]; then
  PURGE_CONFIG=1
  shift
fi
if [[ $# -ne 0 ]]; then
  echo "用法: $0 [--purge-config]" >&2
  exit 2
fi

rm -f -- "${MANIFEST_PATH}" "${HOST_PATH}"
rmdir -- "${INSTALL_ROOT}" 2>/dev/null || true

if [[ ${PURGE_CONFIG} -eq 1 ]]; then
  rm -f -- "${CONFIG_PATH}"
  echo "已删除 Native Host、Firefox manifest 和观察器凭据配置。"
else
  echo "已删除 Native Host 和 Firefox manifest；凭据配置仍保留在 ${CONFIG_PATH}。"
  echo "如需一并删除，请再次运行：$0 --purge-config"
fi
