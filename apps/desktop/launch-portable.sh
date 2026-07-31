#!/usr/bin/env bash
set -euo pipefail

desktop_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
application="${desktop_root}/release/linux-unpacked/centaurai-pocket"
pocket_data_root="${CENTAURAI_POCKET_DATA_DIR:-${HOME}/.local/share/centaurai-pocket}"
pocket_profile_root="${pocket_data_root}/desktop-profile"
launcher_log="${pocket_data_root}/desktop-launcher.log"

if [[ ! -x "${application}" ]]; then
  echo "CentaurAI Pocket 桌面应用尚未构建：${application}" >&2
  echo "请先运行 scripts/build-desktop.sh。" >&2
  exit 1
fi

mkdir -p \
  "${pocket_data_root}" \
  "${pocket_profile_root}/cache" \
  "${pocket_profile_root}/config" \
  "${pocket_profile_root}/pki" \
  "${pocket_profile_root}/share"
chmod 700 "${pocket_data_root}" 2>/dev/null || true
touch "${launcher_log}"
chmod 600 "${launcher_log}" 2>/dev/null || true

launch() {
  if [[ "${CENTAURAI_POCKET_DESKTOP_FOREGROUND:-false}" == "true" ]]; then
    exec "$@"
  fi
  if command -v setsid >/dev/null 2>&1; then
    setsid --fork "$@" </dev/null >>"${launcher_log}" 2>&1
  else
    nohup "$@" </dev/null >>"${launcher_log}" 2>&1 &
  fi
}

# CentaurOS/Ubuntu 的 AppArmor 策略会阻止便携 Electron 使用 Chromium
# user namespace。bubblewrap 提供外层文件系统隔离，因此无需 sudo 或重复输入
# 系统密码；应用只能写入 Pocket 数据目录、会话运行目录和临时目录。
if command -v bwrap >/dev/null 2>&1; then
  bwrap_args=(
    --new-session
    --ro-bind / /
    --dev-bind /dev /dev
    --proc /proc
    --tmpfs /tmp
    --bind "${pocket_data_root}" "${pocket_data_root}"
    --share-net
    --setenv HOME "${HOME}"
    --setenv XDG_CACHE_HOME "${pocket_profile_root}/cache"
    --setenv XDG_CONFIG_HOME "${pocket_profile_root}/config"
    --setenv XDG_DATA_HOME "${pocket_profile_root}/share"
  )
  if [[ -n "${XDG_RUNTIME_DIR:-}" && -d "${XDG_RUNTIME_DIR}" ]]; then
    bwrap_args+=(--bind "${XDG_RUNTIME_DIR}" "${XDG_RUNTIME_DIR}")
  fi
  if [[ -d /tmp/.X11-unix ]]; then
    bwrap_args+=(--ro-bind /tmp/.X11-unix /tmp/.X11-unix)
  fi
  if [[ -d "${HOME}/.pki" ]]; then
    bwrap_args+=(--bind "${pocket_profile_root}/pki" "${HOME}/.pki")
  fi
  launch bwrap \
    "${bwrap_args[@]}" \
    "${application}" \
    --no-sandbox \
    --disable-vulkan \
    --password-store=basic \
    "$@"
  exit 0
fi

echo \
  "当前便携版需要 bubblewrap 提供外层隔离，但系统中未找到 bwrap。" \
  >>"${launcher_log}"
echo "无法安全启动 CentaurAI Pocket；请先安装 bubblewrap。" >&2
exit 1
