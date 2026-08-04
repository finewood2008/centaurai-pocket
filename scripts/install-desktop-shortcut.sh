#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
desktop_root="${project_root}/apps/desktop"
source_launcher="${desktop_root}/launch-portable.sh"
template="${desktop_root}/assets/centaurai-pocket.desktop"
icon_source="${project_root}/apps/mobile/assets/icon.svg"
desktop_filename="ai.centaur.pocket.desktop"
packaged_application="${desktop_root}/release/linux-unpacked"
installed_root="${HOME}/.local/opt/centaurai-pocket"
pocket_data_root="${CENTAURAI_POCKET_DATA_DIR:-${HOME}/.local/share/centaurai-pocket}"
task_execution_origin_file="${pocket_data_root}/task-execution-public-origin"

if [[ ! -x "${packaged_application}/centaurai-pocket" ]]; then
  echo "尚未找到封装后的 Electron 应用，请先运行 scripts/build-desktop.sh。" >&2
  exit 1
fi
for required_file in \
  "release-manifest.sha256" \
  "resources/app.asar" \
  "resources/web/index.html" \
  "resources/assets/icon.png" \
  "resources/backend/centaur-pocket-api"; do
  if [[ ! -f "${packaged_application}/${required_file}" ]]; then
    echo "Electron 应用资源不完整：${required_file}" >&2
    exit 1
  fi
done
(
  cd "${packaged_application}"
  sha256sum --check --strict --quiet release-manifest.sha256
)
if ! command -v bwrap >/dev/null 2>&1; then
  echo "当前便携版需要 bubblewrap，但系统中未找到 bwrap。" >&2
  exit 1
fi

application_version="$(
  node -p "require('${desktop_root}/package.json').version"
)"
release_name="${application_version}-$(date +%Y%m%d%H%M%S)-$$"
release_root="${installed_root}/releases/${release_name}"
installed_launcher="${installed_root}/current/launch-portable.sh"

desktop_directory="$(
  xdg-user-dir DESKTOP 2>/dev/null ||
    printf '%s\n' "${HOME}/Desktop"
)"
applications_directory="${XDG_DATA_HOME:-${HOME}/.local/share}/applications"
icons_directory="${XDG_DATA_HOME:-${HOME}/.local/share}/icons/hicolor/scalable/apps"
menu_entry="${applications_directory}/${desktop_filename}"
desktop_entry="${desktop_directory}/CentaurAI-Pocket.desktop"
installed_icon="${icons_directory}/ai.centaur.pocket.svg"

mkdir -p \
  "${desktop_directory}" \
  "${applications_directory}" \
  "${icons_directory}" \
  "${pocket_data_root}" \
  "${release_root}/release/linux-unpacked"
chmod 0700 "${pocket_data_root}" 2>/dev/null || true

if [[ -n "${CENTAURAI_POCKET_TASK_EXECUTION_PUBLIC_ORIGIN:-}" ]]; then
  canonical_task_execution_origin="$(
    TASK_EXECUTION_ORIGIN="${CENTAURAI_POCKET_TASK_EXECUTION_PUBLIC_ORIGIN}" \
      node <<'NODE'
const value = process.env.TASK_EXECUTION_ORIGIN ?? "";
let parsed;
try {
  parsed = new URL(value);
} catch {
  process.exit(1);
}
if (
  parsed.protocol !== "https:" ||
  parsed.username ||
  parsed.password ||
  (parsed.pathname !== "/" && parsed.pathname !== "") ||
  parsed.search ||
  parsed.hash ||
  value.replace(/\/$/, "") !== parsed.origin ||
  /:443\/?$/.test(value)
) {
  process.exit(1);
}
process.stdout.write(parsed.origin);
NODE
  )" || {
    echo "CENTAURAI_POCKET_TASK_EXECUTION_PUBLIC_ORIGIN 必须是规范 HTTPS Origin。" >&2
    exit 1
  }
  temporary_origin_file="$(mktemp "${pocket_data_root}/.task-execution-origin.XXXXXX")"
  trap 'rm -f "${temporary_origin_file}"' EXIT
  printf '%s\n' "${canonical_task_execution_origin}" >"${temporary_origin_file}"
  chmod 0600 "${temporary_origin_file}"
  mv -f "${temporary_origin_file}" "${task_execution_origin_file}"
  trap - EXIT
fi
cp -a "${packaged_application}/." "${release_root}/release/linux-unpacked/"
install -m 0755 "${source_launcher}" "${release_root}/launch-portable.sh"
test -x "${release_root}/release/linux-unpacked/centaurai-pocket"
test -x \
  "${release_root}/release/linux-unpacked/resources/backend/centaur-pocket-api"

temporary_link="${installed_root}/.current-${release_name}"
ln -s "releases/${release_name}" "${temporary_link}"
mv -Tf "${temporary_link}" "${installed_root}/current"

install -m 0644 "${icon_source}" "${installed_icon}"
install -m 0644 "${template}" "${menu_entry}"

escaped_launcher="${installed_launcher//\\/\\\\}"
escaped_launcher="${escaped_launcher//&/\\&}"
escaped_launcher="${escaped_launcher//|/\\|}"
escaped_icon="${installed_icon//\\/\\\\}"
escaped_icon="${escaped_icon//&/\\&}"
escaped_icon="${escaped_icon//|/\\|}"
sed -i \
  -e "s|@@EXEC@@|${escaped_launcher}|g" \
  -e "s|@@ICON@@|${escaped_icon}|g" \
  "${menu_entry}"

install -m 0755 "${menu_entry}" "${desktop_entry}"
chmod 0755 "${menu_entry}" "${desktop_entry}"

if command -v desktop-file-validate >/dev/null 2>&1; then
  desktop-file-validate "${menu_entry}" "${desktop_entry}"
fi
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "${applications_directory}" >/dev/null 2>&1 || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache \
    -f "${XDG_DATA_HOME:-${HOME}/.local/share}/icons/hicolor" \
    >/dev/null 2>&1 || true
fi
if command -v gio >/dev/null 2>&1; then
  gio set "${desktop_entry}" metadata::trusted true >/dev/null 2>&1 || true
fi

echo "应用菜单入口：${menu_entry}"
echo "桌面快捷方式：${desktop_entry}"
echo "当前用户安装目录：${release_root}"
