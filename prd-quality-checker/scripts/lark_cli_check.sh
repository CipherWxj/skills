#!/usr/bin/env bash
# prd-quality-checker lark-cli 校验脚本：
# 1. 校验 lark-cli 版本。
# 2. 读取本地 auth status，按 expiresAt / refreshExpiresAt 判断登录态。
# 3. 登录态不可用时走 device flow，并尽量自动打开扫码链接。
# 4. 校验通过后写入 ./prd-check-output/.auth-completed 锁。
#
# 退出码：
#   0  可继续执行
#   1  lark-cli 未安装或版本过低
#   2  用户中断鉴权
#   3  device flow 初始化或轮询失败
#
# 输出状态：
#   OK_LOCKED                 锁存在且 token 可用
#   OK_TOKEN_VALID            本地 access token 可用
#   OK_REFRESH_VALID          refresh token 可用，后续 lark-cli 调用可刷新 access token
#   OK_LOGGED_IN              扫码登录完成
#   FAIL_NOT_INSTALLED        lark-cli 未安装
#   FAIL_VERSION <version>    lark-cli 版本过低
#   NEED_SCAN                 需要扫码，下一行会输出 URL
set -u

OUTPUT_DIR="./prd-check-output"
LOCK="$OUTPUT_DIR/.auth-completed"
REQUIRED_VERSION="1.0.20"
# `auth check --scope` 会按顶层 scope 匹配，和 device flow 下发的细分 scope 不一致。
# 这里改用 `auth status` 的 expiresAt / refreshExpiresAt 判活。
SCOPES_LOGIN="docs,drive,wiki"     # `auth login --domain` 使用逗号分隔
SAFETY_WINDOW_SEC=300

extract_json_string() {
  local json="$1"
  local field="$2"
  printf '%s\n' "$json" \
    | grep -oE "\"$field\"[[:space:]]*:[[:space:]]*\"[^\"]+\"" \
    | head -1 \
    | sed -E "s/.*\"$field\"[[:space:]]*:[[:space:]]*\"([^\"]+)\".*/\1/"
}

time_to_epoch() {
  local value="$1"
  date -j -f "%Y-%m-%dT%H:%M:%S" "${value%%+*}" "+%s" 2>/dev/null \
    || date -d "$value" "+%s" 2>/dev/null \
    || echo 0
}

auth_state() {
  local status
  local expires_at
  local refresh_expires_at
  local expires_ts
  local refresh_expires_ts
  local now_ts

  status=$(lark-cli auth status 2>/dev/null || true)
  expires_at=$(extract_json_string "$status" "expiresAt")
  refresh_expires_at=$(extract_json_string "$status" "refreshExpiresAt")
  now_ts=$(date "+%s")

  if [ -n "$expires_at" ]; then
    expires_ts=$(time_to_epoch "$expires_at")
    if [ "$expires_ts" -gt $((now_ts + SAFETY_WINDOW_SEC)) ]; then
      echo "access"
      return 0
    fi
  fi

  if [ -n "$refresh_expires_at" ]; then
    refresh_expires_ts=$(time_to_epoch "$refresh_expires_at")
    if [ "$refresh_expires_ts" -gt $((now_ts + SAFETY_WINDOW_SEC)) ]; then
      echo "refresh"
      return 0
    fi
  fi

  echo "expired"
  return 1
}

# 1) 锁文件快速路径：锁存在时仍校验本地 token，避免使用过期登录态。
if [ -f "$LOCK" ]; then
  AUTH_STATE=$(auth_state || true)
  if [ "$AUTH_STATE" = "access" ]; then
    echo "OK_LOCKED"
    exit 0
  fi
  if [ "$AUTH_STATE" = "refresh" ]; then
    echo "OK_REFRESH_VALID"
    exit 0
  fi
  rm -f "$LOCK"
fi

# 2) 版本校验
LARK_VERSION=$(lark-cli --version 2>&1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)
if [ -z "$LARK_VERSION" ]; then
  echo "FAIL_NOT_INSTALLED"
  echo "Run: npm i -g @larksuite/cli@latest"
  exit 1
fi
if [ "$(printf '%s\n%s\n' "$LARK_VERSION" "$REQUIRED_VERSION" | sort -V | head -1)" != "$REQUIRED_VERSION" ]; then
  echo "FAIL_VERSION $LARK_VERSION"
  echo "Need >= $REQUIRED_VERSION (v2 str_replace support). Run: npm i -g @larksuite/cli@latest"
  exit 1
fi

# 3) 登录态校验：access token 可用则直接通过；refresh token 可用则允许后续调用刷新。
AUTH_STATE=$(auth_state || true)
if [ "$AUTH_STATE" = "access" ]; then
  mkdir -p "$OUTPUT_DIR"
  touch "$LOCK"
  echo "OK_TOKEN_VALID"
  exit 0
fi
if [ "$AUTH_STATE" = "refresh" ]; then
  mkdir -p "$OUTPUT_DIR"
  touch "$LOCK"
  echo "OK_REFRESH_VALID"
  exit 0
fi

# 4) 扫码鉴权：拆成 --no-wait 和 --device-code，便于先打开浏览器再轮询。

# 清理代理，避免 OAuth 请求被公司代理拦截。
export LARK_CLI_NO_PROXY=1
unset HTTPS_PROXY HTTP_PROXY https_proxy http_proxy ALL_PROXY all_proxy

# 4a) 初始化 device flow
INIT_OUT=$(lark-cli auth login --domain "$SCOPES_LOGIN" --no-wait 2>&1)
DEVICE_CODE=$(printf '%s\n' "$INIT_OUT" | grep -oE '"device_code":"[^"]+"' | head -1 | sed 's/.*"device_code":"//;s/"$//')
URL=$(printf '%s\n' "$INIT_OUT" | grep -oE '"verification_url":"[^"]+"' | head -1 | sed 's/.*"verification_url":"//;s/"$//')

if [ -z "$DEVICE_CODE" ] || [ -z "$URL" ]; then
  echo "FAIL_DEVICE_FLOW init rc=$?"
  printf '%s\n' "$INIT_OUT" | head -5 >&2
  exit 3
fi

echo "NEED_SCAN"
echo "URL=$URL"

# 尽量自动打开默认浏览器，失败不阻断流程。
if command -v open >/dev/null 2>&1; then
  open "$URL" >/dev/null 2>&1 || true        # macOS
elif command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$URL" >/dev/null 2>&1 &          # Linux
  disown 2>/dev/null || true
fi

# 4b) 等待扫码完成
if ! lark-cli auth login --device-code "$DEVICE_CODE"; then
  rc=$?
  if [ "$rc" = "130" ]; then
    echo "ABORTED_BY_USER (Ctrl+C)"
    exit 2
  fi
  echo "FAIL_DEVICE_FLOW poll rc=$rc"
  exit 3
fi

mkdir -p "$OUTPUT_DIR"
touch "$LOCK"
echo "OK_LOGGED_IN"
exit 0
