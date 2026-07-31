#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mobile_root="${project_root}/apps/mobile"
eas_cli_version="21.4.0"

usage() {
  cat <<'USAGE'
用法：
  ./scripts/build-mobile.sh
  ./scripts/build-mobile.sh verify
  ./scripts/build-mobile.sh <android|ios|all> <development|preview|production>

不带参数或使用 verify 时，只运行完整的手机端本地校验，不发起云构建。
指定平台与 profile 时，先运行同一套校验，再通过锁定版本的 EAS CLI 发起云构建。

示例：
  ./scripts/build-mobile.sh android development
  ./scripts/build-mobile.sh android preview
  ./scripts/build-mobile.sh android production
  ./scripts/build-mobile.sh ios preview
  ./scripts/build-mobile.sh all production
USAGE
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

platform="${1:-verify}"
profile="${2:-preview}"

if [[ "$#" -gt 2 ]]; then
  usage >&2
  exit 2
fi

case "${platform}" in
  verify)
    if [[ "$#" -gt 1 ]]; then
      echo "verify 模式不接受 profile 参数。" >&2
      usage >&2
      exit 2
    fi
    ;;
  android | ios | all)
    ;;
  *)
    echo "不支持的平台：${platform}" >&2
    usage >&2
    exit 2
    ;;
esac

case "${profile}" in
  development | preview | production)
    ;;
  *)
    echo "不支持的构建 profile：${profile}" >&2
    usage >&2
    exit 2
    ;;
esac

for required_command in node npm; do
  if ! command -v "${required_command}" >/dev/null 2>&1; then
    echo "缺少命令：${required_command}" >&2
    exit 1
  fi
done

echo "[1/6] 校验 EAS JSON"
node -e \
  'const data = JSON.parse(require("node:fs").readFileSync(process.argv[1], "utf8"));
   if (data.cli?.version !== "21.4.0") throw new Error("EAS CLI version is not pinned");
   if (data.build?.preview?.android?.buildType !== "apk") throw new Error("preview must build APK");
   if (data.build?.production?.android?.buildType !== "app-bundle") throw new Error("production must build AAB");' \
  "${mobile_root}/eas.json"

if [[ ! -d "${mobile_root}/node_modules" ]]; then
  echo "[2/6] 安装锁定的手机端依赖"
  npm --prefix "${mobile_root}" ci
else
  echo "[2/6] 校验已安装的手机端依赖"
  npm --prefix "${mobile_root}" ls --depth=0 >/dev/null
fi

echo "[3/6] 图标、Expo 原生配置与 TypeScript 类型检查"
npm --prefix "${mobile_root}" run icons
(
  cd "${mobile_root}"
  npx expo config --type public --json >/dev/null
  npx expo config --type introspect --json >/dev/null
)
npm --prefix "${mobile_root}" run typecheck

echo "[4/6] ESLint 与单元测试"
npm --prefix "${mobile_root}" run lint
npm --prefix "${mobile_root}" test

echo "[5/6] Expo Doctor"
(
  cd "${mobile_root}"
  npx --yes expo-doctor
)

echo "[6/6] Expo Web 导出回归"
npm --prefix "${mobile_root}" run export:web

if [[ "${platform}" == "verify" ]]; then
  echo
  echo "手机端本地校验通过；尚未生成或真机验证 Android/iOS 安装包。"
  exit 0
fi

echo
echo "准备发起 EAS ${platform}/${profile} 云构建。"
echo "EAS 项目归属、Expo 账号以及 Android/iOS 签名凭据必须由产品所有者提供。"

if [[ "${platform}" == "ios" || "${platform}" == "all" ]]; then
  if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "当前不是 macOS：脚本只能请求 EAS 云端构建 iOS，不能在本机运行 Xcode 或安装 IPA。"
  fi
fi

(
  cd "${mobile_root}"
  if ! npx --yes "eas-cli@${eas_cli_version}" whoami; then
    echo "尚未登录 Expo/EAS。请由项目所有者先运行：" >&2
    echo "  cd ${mobile_root} && npx eas-cli@${eas_cli_version} login" >&2
    exit 1
  fi

  npx --yes "eas-cli@${eas_cli_version}" build \
    --platform "${platform}" \
    --profile "${profile}"
)
