# lark-cli 使用约定

本文件只约束 `prd-quality-checker` 使用 `lark-cli` 前的认证校验。PRD 拉取、嵌入展开和产物控制以 `SKILL.md` 为准。

## 1. 使用入口

读取飞书 PRD 前，先执行本 Skill 自带校验脚本：

```bash
bash <skill_path>/scripts/lark_cli_check.sh
```

脚本统一处理版本检查、登录态判活、必要时发起扫码和写入认证锁。不要把 `lark-cli --version`、`auth status`、`auth login` 拆成多条人工命令，也不要直接执行 `lark-cli auth login` 作为兜底。

关键规则：

- 在仓库根目录执行，认证锁固定写入 `./prd-check-output/.auth-completed`。
- 要求 `lark-cli >= 1.0.20`。
- 判活使用 `auth status` 的 `expiresAt` / `refreshExpiresAt`，不用 `auth check --scope`。
- access token 可用时直接通过；access token 不可用但 refresh token 可用时返回 `OK_REFRESH_VALID`，由后续 lark-cli 调用刷新。
- access token 和 refresh token 都不可用，或 refresh token 距离过期不足 5 分钟时，才进入扫码登录。

## 2. 判活规则

脚本只判断本地 lark-cli 登录态是否足以继续执行：

- `expiresAt` 有效：直接继续。
- `expiresAt` 失效但 `refreshExpiresAt` 有效：继续执行，由后续 lark-cli 调用刷新 access token。
- 两者都不可用，或 `refreshExpiresAt` 距离过期不足 5 分钟：进入扫码登录。

## 3. 状态处理

`lark_cli_check.sh` 的 stdout 首行是状态码：

| 状态                        | 处理                                                          |
| --------------------------- | ------------------------------------------------------------- |
| `OK_LOCKED`                 | 锁存在且登录态可用，继续后续 lark-cli 命令                    |
| `OK_TOKEN_VALID`            | access token 可用，继续后续 lark-cli 命令                     |
| `OK_REFRESH_VALID`          | refresh token 可用，继续后续 lark-cli 命令并刷新 access token |
| `NEED_SCAN`                 | 需要扫码，向用户展示脚本输出的 `URL=...`                      |
| `OK_LOGGED_IN`              | 扫码完成，继续后续 lark-cli 命令                              |
| `FAIL_NOT_INSTALLED`        | 停止，安装 `@larksuite/cli`                                   |
| `FAIL_VERSION <version>`    | 停止，升级到 `lark-cli >= 1.0.20`                             |
| `FAIL_DEVICE_FLOW init ...` | 停止，可重跑校验脚本一次                                      |
| `FAIL_DEVICE_FLOW poll ...` | 停止，可重跑校验脚本一次                                      |
| `ABORTED_BY_USER`           | 用户中断扫码，不继续                                          |

## 4. 认证异常处理

后续 lark-cli 命令如果出现认证异常，按以下分支处理：

- `unauthorized`、`expired`、鉴权失败：删除 `./prd-check-output/.auth-completed`，重跑 `lark_cli_check.sh` 后重试一次。
- `Forbidden` 或连接异常：重跑校验脚本；若仍失败，再设置 `LARK_CLI_NO_PROXY=1` 重试一次。
- 权限不足、资源不存在、共享范围不包含当前账号：停止当前读取，提示用户检查链接和权限。

## 5. 禁止事项

- 扫码 URL 只可作为交互提示给用户，不写入报告或持久化产物。
- 禁止展示或记录 device code、token、keychain 路径下的密钥内容。
- 禁止在认证失败后反复 `auth login`；最多按异常分支重跑一次校验脚本。
- 禁止绕过本 Skill 脚本直接改 keychain、配置文件或认证锁。
