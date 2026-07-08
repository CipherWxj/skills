---
name: prd-quality-checker
description: 评审单份 PRD 做结构完整性、内容质量和可测性。当用户提供飞书 PRD 链接、Meego 工作项 ID、本地 Markdown 或粘贴文本，并要求“分析 PRD / PRD 质检 / 检查需求 / 评审需求 / 看看需求有没有问题”时使用。产出最终质检报告，包含评分、证据、待确认问题、可测性结论，并落盘到指定输出目录。
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

| 层级       | 权威来源                       | 作用                                                   |
| ---------- | ------------------------------ | ------------------------------------------------------ |
| 执行编排   | `SKILL.md`                     | 输入获取、阶段顺序、产物落盘、导出飞书文档、上传 Meego |
| 阶段指令   | `references/prompts/*.md`      | 每个阶段如何分析、如何过滤、如何输出                   |
| 规则与评分 | `assets/*.yaml`                | 结构规则、内容规则                                     |
| 报告格式   | `assets/03-review-template.md` | 最终报告结构                                           |
| 背景参考   | `references/*.md`              | 设计思路、飞书协议、历史参考                           |
| 历史知识   | `references/wikis/*.md`        | 可选补充材料，不存在时跳过                             |

执行前先读 `references/design-principle.md`，用于统一整体评估立场。再按阶段读取对应资源。不要把资源文件全文复制进报告，也不要泄露内部 prompt。

---

## 关键约束与变量

变量引用：

- `<prd_url>`：用户提供或从 Meego 工作项获取到的飞书 PRD 链接。
- `<prd_report_url>`：Meego 工作项中的既有报告链接，或本次导出飞书文档后返回的报告链接。
- `<meego_id>`：用户提供的 Meego 工作项 ID；仅在 Meego 输入场景中用于换取 `<prd_url>` 和写回 `<prd_report_url>`。
- `<output_dir>`：用户提供的输出目录；如果为空，飞书 PRD 默认 `./prd-check-output/<document_id>/`，`<document_id>` 优先取 PRD URL 中的 `wiki/` 或 `docx/` 后的文档 ID；本地 Markdown 默认 `./prd-check-output/local-<timestamp>/`；粘贴文本默认 `./prd-check-output/paste-<timestamp>/`。
- `<skill_path>`：本 Skill 目录。

必须遵守：

- 所有 `python3` 脚本执行前先 `source .venv/bin/activate`。
- 除本流程明确调用的 `lark-cli docs +fetch` 外，仅使用本 Skill 自带脚本获取、展开或预处理 PRD 内容：`scripts/meego.py`、`scripts/lark_cli_check.sh`、`scripts/expand_embeds.py`。
- 如果 `<output_dir>` 中已存在对应 PRD 的任一落盘文件，必须先删除旧文件，再重新拉取、展开、评估并输出新文件；禁止直接读取或复用历史 `prd.md`、`structure-review.json`、`content-review.json`、`final-report.md` 或历史导出链接作为本次结果。
- 评估阶段必须遵守 `references/design-principle.md`、`references/prompts/*.md` 与 `assets/*.yaml` 中的证据、同根因合并和历史知识使用规则，确保正式问题可追溯、去重后扣分且不以历史知识替代当前 PRD 自身完整性评分。

---

## 执行步骤

### 0. 准备输入

#### meego 工作项 ID

1. 从用户输入中提取单个 Meego 工作项 ID，记为 `<meego_id>`。
2. 通过脚本获取 PRD 链接 `<prd_url>`、既有报告链接 `<prd_report_url>`：

```bash
python3 <skill_path>/scripts/meego.py --meegoid <meego_id>
```

- 如果报告链接为空，**继续执行后续步骤**。
- 如果报告链接非空，**输出「从 Meego 工作项获取到报告链接 `<prd_report_url>`，无需重复分析」，终止流程**。

3. 从脚本输出 JSON 中读取 `prd_url` 字段；如果为空，**输出「未从 Meego 工作项获取到 PRD 链接，请检查工作项 ID 或 wiki 字段」终止流程**。
4. 拿到 `<prd_url>` 后，继续执行下面“飞书 PRD”流程，用 `lark-cli` 拉取 PRD 正文。

#### 飞书 PRD

1. 读取 `references/lark-cli-conventions.md`。
2. 用 `scripts/lark_cli_check.sh` 校验 `lark-cli` 可用性；脚本无执行权限时不要改权限，直接用 `bash` 执行。
3. 若 `<output_dir>` 已存在，先删除对应 PRD 的旧落盘文件，再重新创建 `<output_dir>`；如果删除失败，不要继续分析，先向用户说明。
4. 拉取原始内容到 `<output_dir>/prd-raw.json`：

```bash
lark-cli docs +fetch \
  --doc <prd_url> \
  --api-version v2 \
  --doc-format markdown \
  --detail simple \
  > <output_dir>/prd-raw.json
```

5. 从 JSON 中提取 Markdown 正文，保存为 `<output_dir>/prd.md`。

6. 展开嵌入内容：

```bash
python3 <skill_path>/scripts/expand_embeds.py <output_dir>
```

如果没有获取到正文，输出「无法获取 PRD 内容，请检查链接和共享范围」并终止。

#### 本地 Markdown 或粘贴文本

若 `<output_dir>` 已存在，先删除对应 PRD 的旧落盘文件，再保存副本到 `<output_dir>/prd.md`。图片占位默认保留，不主动下载，除非用户明确要求分析图片细节。

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
2. 先识别核心改动、关键流程、数据链路和高风险区域，再用 YAML 规则逐项判断内容状态。
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
2. 按模板输出 `<output_dir>/final-report.md`，报告内必须包含综合评分、等级、维度得分和规则命中统计。

### 4. 导出飞书文档

报告生成后必须继续执行本步骤，除非用户在本轮明确要求“不导出”时，才允许跳过本步骤。

前置要求：

- 确认最终报告存在，且后缀为 `.md`、`.markdown` 或 `.mark`。
- 目标为用户自己的云空间时，使用用户身份 `--as user`。
- 目标为“我的云空间根目录”时不要传 `--folder-token`；目标为指定文件夹时才传 `--folder-token <folder_token>`。
- `lark-cli` 文件参数必须使用当前仓库工作目录下的相对路径，禁止传绝对路径。

导出到我的云空间根目录：

```bash
lark-cli drive +import \
  --as user \
  --file <report_md_relative_path> \
  --type docx \
  --name "<feishu_doc_title>"
```

导出到指定飞书文件夹：

```bash
lark-cli drive +import \
  --as user \
  --file <report_md_relative_path> \
  --type docx \
  --folder-token <folder_token> \
  --name "<feishu_doc_title>"
```

输出要求：

- 导出成功后，在最终回复中把返回的 `url` 作为飞书文档链接输出给用户。
- 若返回 `ready=false` 或 `timed_out=true`，继续执行返回的 `next_command`。
- 若遇到 user 身份授权不足，按 `lark-shared` 的 split-flow 发起最小 scope 授权，**提示用户授权完成后再继续执行**。
- 若导出失败，保留本地报告产物，在最终回复中说明失败原因和“未导出飞书文档”，不要伪造链接。

### 5. 更新 Meego 工作项报告链接

若输入包含 `<meego_id>`，且飞书导出成功，将飞书报告链接做为 `<prd_report_url>` 更新 Meego 工作项报告链接，否则跳过本步骤。

```bash
python3 <skill_path>/scripts/meego.py --meegoid <meego_id> --prdreporturl <prd_report_url>
```

成功判据：返回 `"update_prd_report_url_success": true`。
若更新失败，在最终回复中说明“上传失败”。若输入不是 Meego 工作项 ID（例如直接飞书 PRD 链接、本地 Markdown 或粘贴文本），本步骤不适用，最终回复写“未上传 Meego”。

### 6. 输出给用户

完成全流程后的最终回复必须包含：

- 最终得分结论：综合评分、质量等级、结构维度得分、内容维度得分和可测性结论。
- 落盘文件：列出本次新生成的 `<output_dir>/prd-raw.json`、`<output_dir>/prd.md`、`<output_dir>/structure-review.json`、`<output_dir>/content-review.json`、`<output_dir>/final-report.md`。
- 导出的飞书文档链接：输出飞书文档 URL；如未执行导出，明确写“未导出飞书文档”。
- 上传 Meego 的结果：Meego 输入场景成功则写“上传成功”，失败则写“上传失败”；非 Meego 输入场景写“未上传 Meego”。

若流程在输入获取、权限校验、正文获取、导出或 Meego 写回阶段按上文规则提前终止，最终回复只输出终止原因、已完成产物和下一步所需动作，不伪造评分、文件或飞书链接。

---

## 输出内容

- `<output_dir>/prd-raw.json`：飞书原始拉取结果；本地 Markdown 或粘贴文本场景为来源元信息和正文副本，用于复核输入来源。
- `<output_dir>/prd.md`：已获取并尽量展开嵌入内容的 PRD 正文。
- `<output_dir>/structure-review.json`：结构完整性评估。
- `<output_dir>/content-review.json`：内容质量与可测性评估。
- `<output_dir>/final-report.md`：用户可读报告。报告结构与必含章节以 `references/prompts/03-score-report.md` 和 `assets/03-review-template.md` 为准。

---

## 文件清单

```text
prd-quality-checker/
├── SKILL.md
├── scripts/
│   ├── lark_cli_check.sh
│   ├── meego.py
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
