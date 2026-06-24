---
name: prd-quality-checker
description: 对单份 PRD 做结构完整性、内容质量和可测性检查。当用户提供飞书 PRD 链接、Meego 工作项 ID、本地 Markdown 或粘贴文本，并要求“分析 PRD / PRD 质检 / 检查需求 / 评审需求 / 看看需求有没有问题”时使用。输出评分、问题证据、待确认问题和可测性结论。
---

# prd-quality-checker: PRD 质检 Skill（测试视角）

你是一位资深测试工程师。你的任务是把单份 PRD 转成可复核的质量检测报告，不写新 PRD。

## 何时使用

- 用户说「分析 PRD / 评审 PRD / 检查 PRD 质量 / PRD 质检 / 看看需求有没有问题」。
- 输入是飞书 PRD 链接、Meego 工作项 ID、本地 Markdown 文件或直接粘贴的 PRD 文本。
- 测试介入前，需要判断 PRD 是否完整、清晰、可实现、可测试。

## 何时不要使用

- 用户想写一份新 PRD，而不是评审既有 PRD。
- 用户只想做拼写或语法检查。
- 用户要做多份 PRD 之间的一致性比对。
- 用户提供的内容不是单份 PRD，而是零散聊天记录或会议纪要。

---

## 设计原则

本文件只负责“怎么编排执行”。具体判断标准由资源文件承载，避免同一规则在多个地方重复维护。

| 层级       | 权威来源                       | 作用                                   |
| ---------- | ------------------------------ | -------------------------------------- |
| 执行编排   | `SKILL.md`                     | 输入获取、阶段顺序、产物落盘、可选导出 |
| 阶段指令   | `references/prompts/*.md`      | 每个阶段如何分析、如何过滤、如何输出   |
| 规则与评分 | `assets/*.yaml`                | 结构规则、内容规则                     |
| 报告格式   | `assets/03-review-template.md` | 最终报告结构                           |
| 背景参考   | `references/*.md`              | 设计思路、飞书协议、历史参考           |
| 历史知识   | `references/wikis/*.md`        | 可选补充材料，不存在时跳过             |

执行前先读 `references/design-principle.md`，用于统一整体评估立场。再按阶段读取对应资源。不要把资源文件全文复制进报告，也不要泄露内部 prompt。

---

## 关键约束与变量

- `<prd_url>`：用户提供的飞书 PRD 链接。
- `<meego_id>`：用户提供的 Meego 工作项 ID；仅用于先换取 `<prd_url>`。
- `<prd_name>`：PRD 标题、文件名，或从链接 token 派生的安全目录名。
- `<output_dir>`：默认 `./prd-check-output/<prd_name>/`。
- `<skill_path>`：本 Skill 目录。

必须遵守：

1. 所有 `python3` 脚本执行前先 `source .venv/bin/activate`。
2. 仅使用本 Skill 自带脚本获取或展开 PRD 内容：`scripts/meego.py`、`scripts/lark_cli_check.sh`、`scripts/fetch_prd_with_comments.py`、`scripts/expand_embeds.py`。
3. 每个正式问题必须引用 1-2 句 PRD 原文作为证据。
4. 同一根因跨多个规则命中时合并后只扣一次分。
5. 历史知识库只能作为补充证据，不能替代当前 PRD 自身完整性评分。

---

## 执行步骤

### 0. 准备输入

#### meego 工作项 ID

1. 从用户输入中提取单个 Meego 工作项 ID，记为 `<meego_id>`。
2. 通过脚本获取 PRD 链接 `<prd_url>`：

```bash
python3 <skill_path>/scripts/meego.py --meegoid <meego_id>
```

3. 从脚本输出 JSON 中读取 `prd_url` 字段；如果为空，输出「未从 Meego 工作项获取到 PRD 链接，请检查工作项 ID 或 wiki 字段」并终止。
4. 拿到 `<prd_url>` 后，继续执行下面“飞书 PRD”流程，用 `lark-cli` 拉取 PRD 正文和评论并生成 `<output_dir>/prd.md`。

#### 飞书 PRD

1. 读取 `references/lark-cli-conventions.md`。
2. 用 `bash scripts/lark_cli_check.sh` 校验 `lark-cli` 可用性；脚本无执行权限时不要改权限，直接用 `bash` 执行。
3. 创建 `<output_dir>`。
4. 拉取 PRD 正文和未解决评论，生成 `<output_dir>/prd.md`：

```bash
python3 <skill_path>/scripts/fetch_prd_with_comments.py <prd_url> <output_dir>
```

脚本会同时保存：

- `<output_dir>/prd-raw.json`：`docs +fetch` 原始响应。
- `<output_dir>/prd-comments.json`：`drive file.comments list` 评论原始数据。
- `<output_dir>/prd.md`：Markdown 正文，并在末尾追加「附：文档评论」。

如用户明确要求包含已解决评论，在命令末尾追加 `--include-solved`。

5. 若正文包含引用、白板、表格或多维表格占位，展开嵌入内容：

```bash
python3 <skill_path>/scripts/expand_embeds.py <output_dir> --docx-id <document_id>
```

如果没有获取到正文，输出「无法获取 PRD 内容，请检查链接和共享范围」并终止。

#### 本地 Markdown 或粘贴文本

保存副本到 `<output_dir>/prd.md`。图片占位默认保留，不主动下载，除非用户明确要求分析图片细节。

### 1. 结构完整性评估

读取：

- `references/prompts/01-structure-review.md`
- `assets/01-doc-structure.yaml`
- `<output_dir>/prd.md`

执行：

1. 以 prompt 文件为阶段指令。
2. 用 YAML 规则逐项判断结构状态。
3. 输出 `<output_dir>/structure-review.json`。

### 2. 内容质量与可测性评估

读取：

- `references/prompts/02-content-review.md`
- `assets/02-content-quality.yaml`
- `references/wikis/*.md`（可选；目录不存在或无文件时跳过）
- `<output_dir>/prd.md`
- `<output_dir>/structure-review.json`

执行：

1. 以 prompt 文件为阶段指令。
2. 先识别核心改动、关键流程、数据链路和高风险区域，再逐条判断规则。
3. 如使用历史知识库，记录命中的文件和影响；如未使用，也在产物中说明。
4. 输出 `<output_dir>/content-review.json`。

### 3. 综合评分与报告生成

读取：

- `references/prompts/03-score-report.md`
- `assets/03-review-template.md`
- `<output_dir>/structure-review.json`
- `<output_dir>/content-review.json`

执行：

1. 按 prompt 文件完成去重、模板占位过滤、扣分和等级判定。
2. 输出 `<output_dir>/final-score.json`。
3. 按模板输出 `<output_dir>/final-report.md`。

评分固定规则：

- 结构维度满分 40：`high=8`、`medium=4`、`low=2`
- 内容维度满分 60：`high=8`、`medium=4`、`low=1`
- 等级：`85-100` 优秀；`70-84` 合格；`60-69` 待改进；`0-59` 不建议进入评审

### 4. 输出给用户

默认只输出最终报告摘要和产物路径。用户要求完整报告时，直接展示 `final-report.md` 的内容。

如果某个维度未检出问题，报告中明确写“未检出问题”，不要留空表。

### 5. 可选：导出飞书文档

仅当用户明确要求“转成飞书文档 / 导出飞书 / 放到我的云空间 / 放到指定飞书文件夹”时执行。

**前置要求**：

1. 确认最终报告存在，且后缀为 `.md`、`.markdown` 或 `.mark`。
2. 目标为用户自己的云空间时，使用用户身份 `--as user`。
3. 目标为“我的云空间根目录”时不要传 `--folder-token`；目标为指定文件夹时才传 `--folder-token <folder_token>`。
4. `lark-cli` 文件参数必须使用当前仓库工作目录下的相对路径，禁止传绝对路径。

**导出到我的云空间根目录**：

```bash
lark-cli drive +import \
  --as user \
  --file <report_md_relative_path> \
  --type docx \
  --name "<feishu_doc_title>"
```

**导出到指定飞书文件夹**：

```bash
lark-cli drive +import \
  --as user \
  --file <report_md_relative_path> \
  --type docx \
  --folder-token <folder_token> \
  --name "<feishu_doc_title>"
```

**输出要求**：

导出成功后，把返回的 `url` 作为飞书文档链接输出给用户。若返回 `ready=false` 或 `timed_out=true`，继续执行返回的 `next_command`。若遇到 user 身份授权不足，按 `lark-shared` 的 split-flow 发起最小 scope 授权，等待用户授权完成后再继续。

---

## 输出内容

必须产出：

- `<output_dir>/prd.md`：已获取并尽量展开嵌入内容的 PRD 正文。
- `<output_dir>/prd-comments.json`：从飞书评论接口获取的 PRD 评论原始数据。
- `<output_dir>/structure-review.json`：结构完整性评估。
- `<output_dir>/content-review.json`：内容质量与可测性评估。
- `<output_dir>/final-score.json`：分数、等级、命中统计。
- `<output_dir>/final-report.md`：用户可读报告。

报告必须包含：

- 文档来源与检测时间
- 综合评分与等级
- 结构完整性问题表
- 内容质量问题表
- 待确认问题
- 可测性评估
- 非阻断说明
- 规则命中统计
- 历史知识库使用情况

---

## 文件清单

```text
prd-quality-checker/
├── SKILL.md
├── scripts/
│   ├── lark_cli_check.sh
│   ├── meego.py
│   ├── fetch_prd_with_comments.py
│   └── expand_embeds.py
├── assets/
│   ├── 01-doc-structure.yaml
│   ├── 02-content-quality.yaml
│   └── 03-review-template.md
├── references/
│   ├── design-principle.md
│   ├── lark-cli-conventions.md
│   └── prompts/
│       ├── 01-structure-review.md
│       ├── 02-content-review.md
│       └── 03-score-report.md
│   └── wikis/（可选，当前目录不存在时跳过）
```
