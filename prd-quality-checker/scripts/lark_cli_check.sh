#!/usr/bin/env bash
# prd-quality-checker lark_cli_check — single sandbox-out shell call that does:
#   1) lark-cli version check (sort -V semantic; reject < 1.0.20)
#   2) auth status (millisecond local keychain read; check expiresAt with 5min safety window)
#   3) if token expired / no token → split device flow:
#        a) `auth login --domain X --no-wait` returns JSON{device_code, verification_url}
#        b) auto-open URL via `open` (macOS) / `xdg-open` (linux); print as fallback
#        c) `auth login --device-code <code>` blocks until scan completes; writes keychain
#      Split form is required so we can `open` the URL — blocking form prints
#      then immediately polls, leaving no chance to grab the URL first.
#   4) touch ./prd-check-output/.auth-completed lock so subsequent calls short-circuit
#
# Agent contract:
#   - Call this script ONCE per session via Shell tool with
#     required_permissions: ["all"] (sandbox-out is required because step 3
#     writes lark-cli token to macOS keychain / ~/.config/lark-cli/*.enc).
#   - Stream stdout so you can show the verification URL to the user.
#   - Exit codes:
#       0  → ready (token valid, lock written)
#       1  → lark-cli not installed or version too old
#       2  → user aborted device flow (Ctrl+C)
#       3  → device flow timed out (10 min default)
#
# Output protocol (one line per state for easy grep):
#   OK_LOCKED                 lock existed, fast path
#   OK_TOKEN_VALID            auth check passed, lock written
#   OK_LOGGED_IN              device flow completed, lock written
#   FAIL_NOT_INSTALLED        lark-cli missing
#   FAIL_VERSION <version>    too old
#   NEED_SCAN                 about to invoke device flow (URL on next lines)
set -u

OUTPUT_DIR="./prd-check-output"
LOCK="$OUTPUT_DIR/.auth-completed"
REQUIRED_VERSION="1.0.20"
# NB: lark-cli `auth login --domain` is comma-separated; `auth check --scope` (space-sep)
# is deprecated for our use because it requires top-level scope names (docs/drive/wiki)
# while device-flow actually grants细分 scopes（docs:document.content:read 等）—— always
# returns ok:false. We use `auth status` (no flag) + parse `expiresAt` instead.
SCOPES_LOGIN="docs,drive,wiki"     # comma-separated for `auth login --domain`

# 1) Lock short-circuit -------------------------------------------------------
# Lock 命中时再用 `auth status` 校验 token 真的没过期。
#
# 为什么不用 `auth check --scope`：lark-cli 现版本 `auth check --scope "docs drive wiki"`
# 是顶层 scope 名严格匹配，但 device flow 实际颁发的是细分 scope（`docs:document.content:read`
# / `wiki:node:read` ...）。三个顶层名永远在 `missing` 里 → 永远返回 ok:false →
# lock 短路前校验必然失败 → 每次跑 lark_cli_check 都触发新一轮 device flow。
#
# `auth status` 是本地 keychain 读取（无网络）：
#   { "expiresAt": "...", "identity": "user", "scope": "<细分 scope 列表>" }
# 取 expiresAt + 留 5 分钟时间安全窗，过期即删 lock 落到 step 3 重新登录。
if [ -f "$LOCK" ]; then
  EXPIRES_AT=$(lark-cli auth status 2>/dev/null | grep -oE '"expiresAt"[[:space:]]*:[[:space:]]*"[^"]+"' | sed -E 's/.*"expiresAt"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/')
  if [ -n "$EXPIRES_AT" ]; then
    EXPIRES_TS=$(date -j -f "%Y-%m-%dT%H:%M:%S" "${EXPIRES_AT%%+*}" "+%s" 2>/dev/null || date -d "$EXPIRES_AT" "+%s" 2>/dev/null || echo 0)
    NOW_TS=$(date "+%s")
    # 300s 安全窗：access token 过期前 5 分钟就当过期，避免 fetch 拿到 token 后才发现刚好过期
    if [ "$EXPIRES_TS" -gt $((NOW_TS + 300)) ]; then
      echo "OK_LOCKED"
      exit 0
    fi
  fi
  rm -f "$LOCK"
fi

# 2) Version check ------------------------------------------------------------
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

# 3) Auth status (millisecond local keychain read; no network) ----------------
# 跟 step 1 同理：用 `auth status` 取 expiresAt 判活，不用 `auth check --scope`
# （后者顶层 scope 名匹配 vs 实际细分 scope 颁发不匹配，永远 ok:false）。
EXPIRES_AT=$(lark-cli auth status 2>/dev/null | grep -oE '"expiresAt"[[:space:]]*:[[:space:]]*"[^"]+"' | sed -E 's/.*"expiresAt"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/')
if [ -n "$EXPIRES_AT" ]; then
  EXPIRES_TS=$(date -j -f "%Y-%m-%dT%H:%M:%S" "${EXPIRES_AT%%+*}" "+%s" 2>/dev/null || date -d "$EXPIRES_AT" "+%s" 2>/dev/null || echo 0)
  NOW_TS=$(date "+%s")
  if [ "$EXPIRES_TS" -gt $((NOW_TS + 300)) ]; then
    mkdir -p "$OUTPUT_DIR"
    touch "$LOCK"
    echo "OK_TOKEN_VALID"
    exit 0
  fi
fi

# 4) Device flow ---------------------------------------------------------------
# Why we split --no-wait + --device-code instead of using blocking --domain:
#   We need to AUTO-OPEN the browser. Blocking mode prints the URL THEN
#   immediately polls and blocks — we have no chance to grab the URL and
#   `open` it before lark-cli is already waiting. The split form lets us:
#     4a) --no-wait               returns JSON with device_code + verification_url
#         → auto-open URL in browser, also print it as fallback
#     4b) --device-code <code>    blocks until user finishes scan; writes keychain

# Strip company HTTPS proxy — Feishu OAuth endpoints 403 through most corp
# proxies; LARK_CLI_NO_PROXY=1 is the documented escape hatch (>=0.18).
export LARK_CLI_NO_PROXY=1
unset HTTPS_PROXY HTTP_PROXY https_proxy http_proxy ALL_PROXY all_proxy

# 4a) Initiate device flow (non-blocking) ----
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

# Auto-open in default browser (best effort — never block on failure)
if command -v open >/dev/null 2>&1; then
  open "$URL" >/dev/null 2>&1 || true        # macOS
elif command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$URL" >/dev/null 2>&1 &          # Linux
  disown 2>/dev/null || true
fi

# 4b) Poll until user finishes scanning -----
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
