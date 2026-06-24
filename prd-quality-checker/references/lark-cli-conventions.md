# lark-cli 使用约定

本文件只约束 `prd-quality-checker` 获取飞书 PRD 内容时的 `lark-cli` 用法。执行步骤以 `SKILL.md` 的“准备输入 / 飞书 PRD”为准。

## 1. 使用入口

读取飞书 PRD 前，先执行本 Skill 自带校验脚本：

```bash
bash <skill_path>/scripts/lark_cli_check.sh
```

约束：

- 在仓库根目录执行，确保认证锁写入 `./prd-check-output/.auth-completed`。
- 脚本可能写入本机 keychain 或 `~/.config/lark-cli/*.enc`，需要允许完整本地权限。
- 不要把 `lark-cli --version`、`auth status`、`auth login` 拆成多条人工命令，版本检查、token 判活、登录和写锁统一交给脚本处理。
- 不要直接执行 `lark-cli auth login` 作为失败兜底，避免重复扫码和一次性 device code 被消费。

脚本行为：

- 要求 `lark-cli >= 1.0.20`。
- 认证锁存在时仍会读取 `auth status`，确认 token 未过期才返回 `OK_LOCKED`。
- token 距离过期不足 5 分钟时按过期处理，删除旧锁并重新登录。
- 登录使用 `auth login --domain docs,drive,wiki --no-wait` 获取扫码 URL，脚本会尝试自动打开浏览器，并打印 `URL=<verification_url>` 作为兜底。
- 脚本会设置 `LARK_CLI_NO_PROXY=1` 并清理常见代理变量，避免 OAuth 请求被公司代理拦截。

## 2. 成功后再拉取 PRD

校验通过后，回到 `SKILL.md` 的“准备输入 / 飞书 PRD”步骤执行 `scripts/fetch_prd_with_comments.py`。该脚本负责拉取正文、读取评论，并生成最终 Markdown。本文件不重复维护具体命令，避免与执行流程产生双源不一致。

如果提取出的正文为空，停止后续评审，提示“无法获取 PRD 内容，请检查链接和共享范围”。

## 3. 脚本状态处理

`lark_cli_check.sh` 的 stdout 首行是状态码：

| 状态                        | 处理                                                                     |
| --------------------------- | ------------------------------------------------------------------------ |
| `OK_LOCKED`                 | 认证锁有效且 token 未临近过期，继续执行 `fetch_prd_with_comments.py`     |
| `OK_TOKEN_VALID`            | 本地 token 有效，脚本已写入认证锁，继续执行 `fetch_prd_with_comments.py` |
| `NEED_SCAN`                 | 脚本已输出 `URL=...` 并尝试自动打开浏览器；等待用户扫码，脚本结束后继续  |
| `OK_LOGGED_IN`              | 扫码完成，脚本已写入认证锁，继续执行 `fetch_prd_with_comments.py`        |
| `FAIL_NOT_INSTALLED`        | 停止，提示安装 `@larksuite/cli`                                          |
| `FAIL_VERSION <version>`    | 停止，提示升级到 `lark-cli >= 1.0.20`                                    |
| `FAIL_DEVICE_FLOW init ...` | 停止，说明未拿到 device code 或扫码 URL；可重跑校验脚本一次              |
| `FAIL_DEVICE_FLOW poll ...` | 停止，说明扫码轮询失败或超时；可重跑校验脚本一次                         |
| `ABORTED_BY_USER`           | 用户中断扫码，不继续拉取文档                                             |

## 4. PRD 拉取异常处理

执行 `fetch_prd_with_comments.py` 后按结果分支：

- 鉴权失败、token 过期、`unauthorized`、`expired`：删除 `./prd-check-output/.auth-completed`，重跑 `lark_cli_check.sh`，通过后重试一次 PRD 拉取。
- 权限不足、文档不存在、共享范围不包含当前账号：不要继续评分，提示用户检查链接、访问权限或共享范围。
- 网络代理导致 `Forbidden` 或连接异常：优先重跑校验脚本；脚本已清理代理变量。若 PRD 拉取仍失败，再在当前 shell 设置 `LARK_CLI_NO_PROXY=1` 并重试一次。
- 返回 JSON 无正文或正文只有空白：不要生成空报告，提示无法获取 PRD 内容。
- PRD 正文和评论拉取成功但嵌入内容无法展开：保留已获取正文和评论继续评估，并在报告非阻断说明中记录不可读取的引用、白板或表格。

## 5. 禁止事项

- 扫码 URL 只可作为交互提示给用户，不写入报告或持久化产物。
- 禁止展示或记录 device code、token、keychain 路径下的密钥内容。
- 禁止在认证失败后反复 `auth login`；最多按异常分支重跑一次校验脚本。
- 禁止绕过本 Skill 脚本直接改 keychain、配置文件或认证锁。
- 禁止因嵌入内容部分失败而丢弃已获取的 PRD 正文。
