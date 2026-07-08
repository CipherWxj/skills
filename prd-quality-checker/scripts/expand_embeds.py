#!/usr/bin/env python3
"""飞书嵌入式占位展开：把 prd.md 里 4 类占位调对应 lark-cli 拼成 markdown 就地替换；
从 prd-raw.json 读取 docx_id 后，再用 docx blocks API 扫一遍 block_type=53（docs +fetch 静默丢失的嵌入式
多维表格视图），append 到 prd.md 末尾「附：嵌入式多维表格」段；同时拉取普通正文评论，
并按引用 block 插入到 PRD 原文附近，过滤画板/表格/多维表格等非正文评论。

用法：python3 expand_embeds.py <WORK_DIR>
失败保留原占位、不阻断主流程；默认不保留调试中间文件。
需要排查时可加 --debug-artifacts，额外写入 prd.md.before-expand、评论 JSON 和 embeds-failed.jsonl。
"""

from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

LARK_TIMEOUT_SEC = 20
CITE_TRUNCATE = (
    1500  # cite 只展一层；1500 字够覆盖"为什么链过来"的语境，避免 token 爆炸
)
BITABLE_LIMIT = 200  # 当前 PRD 多维表格典型 20-50 条，10× safety margin
BLOCK_TYPE_EMBEDDED_BITABLE = 53  # docx v1 blocks API 中嵌入式多维表格视图块

# 4 类占位标签的正则（CDATA 自闭合 + 普通闭合两种 lark 都吐过）
EMBED_PATTERNS = {
    "base_refer": re.compile(r"<base_refer\s+([^>]*?)(?:></base_refer>|/>)"),
    "sheet": re.compile(r"<sheet\s+([^>]*?)(?:></sheet>|/>)"),
    "cite": re.compile(r"<cite\s+([^>]*?)(?:></cite>|/>)"),
    "whiteboard": re.compile(r"<whiteboard\s+([^>]*?)(?:></whiteboard>|/>)"),
}

# attr1="value" attr2="value2" … 形态的属性解析
ATTR_RE = re.compile(r'(\w[\w-]*)="([^"]*)"')

# 飞书业务错误码：无权限访问目标文档
ERR_NO_PERMISSION = 3380004


def parse_attrs(attr_str: str) -> dict[str, str]:
    return {m.group(1): m.group(2) for m in ATTR_RE.finditer(attr_str)}


def first_attr(attrs: dict[str, str], *names: str) -> str | None:
    """按候选名读取属性，兼容 token/doc-id、kebab/snake 等飞书导出差异。"""
    for name in names:
        value = attrs.get(name)
        if value:
            return value
    return None


def parse_lark_json(stdout: bytes) -> dict | None:
    """剥离 lark-cli stdout 的非 JSON 前缀（'Resolving wiki node: …' 等）后解析。"""
    text = stdout.decode("utf-8", errors="replace")
    start = text.find("{")
    if start < 0:
        return None
    try:
        return json.loads(text[start:])
    except json.JSONDecodeError:
        return None


def lark_call(args: list[str]) -> tuple[int, dict | None, bytes]:
    """调 lark-cli。返回 (returncode, parsed_json_or_None, raw_combined_for_diag)。

    某些子命令（如 docs +fetch）在 rc != 0 时把 JSON envelope 写到 stderr 而不是 stdout，
    所以这里两个流都尝试解析。
    """
    try:
        r = subprocess.run(args, capture_output=True, timeout=LARK_TIMEOUT_SEC)
        d = parse_lark_json(r.stdout) or parse_lark_json(r.stderr)
        diag = r.stdout if r.stdout else r.stderr
        return r.returncode, d, diag
    except subprocess.TimeoutExpired:
        return -1, None, b"timeout"
    except Exception as exc:  # noqa: BLE001
        return -1, None, repr(exc).encode("utf-8")


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def normalize_body_line(line: str) -> str:
    return normalize_text(html.unescape(re.sub(r"<[^>]+>", "", line)))


# ---------- 4 类 handler ----------


def expand_bitable(attrs: dict[str, str]) -> tuple[str, str | None]:
    """多维表格 → markdown 表（CLI 直接吐 markdown）。"""
    token = attrs.get("token")
    table_id = attrs.get("table-id") or attrs.get("table_id")
    view_id = attrs.get("view-id") or attrs.get("view_id")
    if not (token and table_id):
        return "", f"missing-attrs token={token!r} table-id={table_id!r}"

    args = [
        "lark-cli",
        "base",
        "+record-list",
        "--base-token",
        token,
        "--table-id",
        table_id,
        "--limit",
        str(BITABLE_LIMIT),
        "--format",
        "markdown",
    ]
    if view_id:
        args.extend(["--view-id", view_id])

    try:
        r = subprocess.run(args, capture_output=True, timeout=LARK_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        return "", "timeout"
    except Exception as exc:  # noqa: BLE001
        return "", f"exception: {exc!r}"

    if r.returncode != 0:
        # 失败时 envelope 可能在 stdout 或 stderr；提取业务码做差异化
        d = parse_lark_json(r.stdout) or parse_lark_json(r.stderr)
        if d and (d.get("error") or {}).get("code") == ERR_NO_PERMISSION:
            return "_（多维表格：无权限访问）_", None
        msg = (d or {}).get("error", {}).get("message") if d else None
        if not msg:
            msg = (r.stdout or r.stderr).decode("utf-8", "replace")[:200]
        return "", f"cli-fail rc={r.returncode}: {msg}"

    md = r.stdout.decode("utf-8", "replace").strip()
    # 首行可能是 `_record_id is metadata for record operations…` 的 CLI 提示
    lines = md.splitlines()
    if lines and lines[0].startswith("`_record_id` is metadata"):
        lines = lines[1:]
        while lines and lines[0].strip() == "":
            lines = lines[1:]
    return "\n".join(lines), None


def expand_sheet(attrs: dict[str, str]) -> tuple[str, str | None]:
    """电子表格 → markdown 表（脚本拼装 valueRange.values 二维数组）。"""
    token = attrs.get("token")
    sheet_id = attrs.get("sheet-id") or attrs.get("sheet_id")
    if not (token and sheet_id):
        return "", f"missing-attrs token={token!r} sheet-id={sheet_id!r}"

    rc, d, raw = lark_call(
        [
            "lark-cli",
            "sheets",
            "+read",
            "--spreadsheet-token",
            token,
            "--range",
            f"{sheet_id}!A1:Z1000",
        ]
    )
    if d and not d.get("ok"):
        if (d.get("error") or {}).get("code") == ERR_NO_PERMISSION:
            return "_（电子表格：无权限访问）_", None
        return "", f"api-fail: {(d.get('error') or {}).get('message', 'unknown')}"
    if rc != 0 or not d:
        return "", f"cli-fail rc={rc}: {raw[:200]!r}"

    values = (d.get("data") or {}).get("valueRange", {}).get("values") or []
    # strip 尾部全空行
    while values and all(c in (None, "") for c in values[-1]):
        values.pop()
    if not values:
        return "_（空表格）_", None

    # 算实际宽度：剔除尾部全空列（A1:Z1000 拉过宽时尤其明显）
    max_width = max(len(row) for row in values)
    cols = max_width
    while cols > 1 and all(
        cols - 1 >= len(row) or row[cols - 1] in (None, "") for row in values
    ):
        cols -= 1

    def cell(v: object) -> str:
        if v is None:
            return ""
        return str(v).replace("|", "\\|").replace("\n", " ")

    def trim(row: list) -> list:
        # 截到 cols 宽度（补 None / 截尾）
        return (row + [None] * cols)[:cols]

    lines = [
        "| " + " | ".join(cell(c) for c in trim(values[0])) + " |",
        "| " + " | ".join("---" for _ in range(cols)) + " |",
    ]
    for row in values[1:]:
        lines.append("| " + " | ".join(cell(c) for c in trim(row)) + " |")
    return "\n".join(lines), None


def expand_cite(attrs: dict[str, str], host: str) -> tuple[str, str | None]:
    """引用其他飞书文档 → markdown 摘要（≤CITE_TRUNCATE 字）。"""
    cite_type = attrs.get("type")
    if cite_type == "user":
        user_name = first_attr(attrs, "user-name", "user_name", "name")
        user_id = first_attr(attrs, "user-id", "user_id")
        if user_name:
            return f"@{user_name}", None
        if user_id:
            return f"_（用户引用：{user_id}）_", None
        return "", "missing-attrs user-name/user-id"

    token = first_attr(attrs, "token", "doc-id", "doc_id", "url")
    file_type = first_attr(attrs, "file-type", "file_type") or "docx"
    title = attrs.get("title") or token
    if not token:
        return "", "missing-attrs token/doc-id"

    url = (
        token
        if token.startswith(("http://", "https://"))
        else f"{host}/{file_type}/{token}"
    )
    rc, d, raw = lark_call(
        [
            "lark-cli",
            "docs",
            "+fetch",
            "--doc",
            url,
            "--api-version",
            "v2",
            "--doc-format",
            "markdown",
            "--detail",
            "simple",
        ]
    )
    if d and not d.get("ok"):
        if (d.get("error") or {}).get("code") == ERR_NO_PERMISSION:
            return f"_（引用文档「{title}」：无权限访问）_", None
        return "", f"api-fail: {(d.get('error') or {}).get('message', 'unknown')}"
    if rc != 0 or not d:
        return "", f"cli-fail rc={rc}: {raw[:200]!r}"

    content = (d.get("data") or {}).get("document", {}).get("content") or ""
    header = f"_引用文档「{title}」（{file_type}/{token}）摘要：_\n\n"
    if len(content) > CITE_TRUNCATE:
        return (
            header + content[:CITE_TRUNCATE] + f"\n\n_（已截前 {CITE_TRUNCATE} 字）_",
            None,
        )
    return header + content, None


def expand_whiteboard(attrs: dict[str, str]) -> tuple[str, str | None]:
    """白板 → 占位文案（OpenAPI 不暴露文字内容）。"""
    token = attrs.get("token", "?")
    return f"_（白板 {token}：内容无法通过 OpenAPI 读取）_", None


def discover_embedded_bitables_via_blocks(docx_id: str) -> list[dict[str, str]]:
    """扫 docx 的 block 列表，挑出 block_type=53（嵌入式多维表格视图）。

    这是 docs +fetch markdown/xml/v1/raw_content 全部静默丢失的块类型，
    要通过 docx blocks API 才能看到 reference_base.token（格式 `{app_token}_{table_id}`）+ view_id。

    返回 [{"token": "<app_token>_<table_id>", "table-id": "<tbl_xxx>", "view-id": "<vew_xxx>"}, ...]
    （key 与 4 类占位 attrs 一致，可直接喂 expand_bitable）。
    """
    rc, d, _raw = lark_call(
        [
            "lark-cli",
            "api",
            "GET",
            f"/open-apis/docx/v1/documents/{docx_id}/blocks",
            "--page-size",
            "500",
        ]
    )
    # 注意：lark-cli api GET 是 raw API 透传，飞书原始响应顶层是 {code, data, msg}，
    # 没有其他子命令的 `ok` 字段；成功用 code==0 判定。
    if rc != 0 or not d or d.get("code") not in (0, None):
        return []
    out: list[dict[str, str]] = []
    for b in (d.get("data") or {}).get("items", []) or []:
        if b.get("block_type") != BLOCK_TYPE_EMBEDDED_BITABLE:
            continue
        ref = b.get("reference_base") or {}
        full_token = ref.get("token") or ""
        # token 格式约定：`{app_token}_{table_id}`，按第一个下划线切
        if "_" not in full_token:
            continue
        app_token, table_id = full_token.split("_", 1)
        out.append(
            {
                "token": app_token,
                "table-id": table_id,
                "view-id": ref.get("view_id") or "",
            }
        )
    return out


# ---------- 普通正文评论处理 ----------


def extract_reply_text(reply: dict) -> str:
    parts: list[str] = []
    for element in reply.get("reply_elements") or []:
        if not isinstance(element, dict):
            continue
        if element.get("type") == "text":
            parts.append(str(element.get("text") or ""))
        elif element.get("text"):
            parts.append(str(element["text"]))

    for element in (reply.get("content") or {}).get("elements") or []:
        if not isinstance(element, dict):
            continue
        text_run = element.get("text_run") or {}
        person = element.get("person") or {}
        docs_link = element.get("docs_link") or {}
        if text_run.get("text"):
            parts.append(str(text_run["text"]))
        elif person.get("user_id"):
            parts.append(f"@{person['user_id']}")
        elif docs_link.get("url"):
            title = docs_link.get("title") or docs_link["url"]
            parts.append(f"[{title}]({docs_link['url']})")

    for image in (reply.get("extra") or {}).get("image_list") or []:
        parts.append(f"\n  - 图片附件 token：`{image}`")

    return "".join(parts).strip()


def extract_comment_block_id(comment: dict) -> str:
    relation_text = (comment.get("relation") or {}).get("relation")
    if not relation_text:
        return ""
    try:
        relation_json = json.loads(relation_text)
    except json.JSONDecodeError:
        return ""
    for item in relation_json.values():
        block_id = (item.get("positionInfo") or {}).get("blockID")
        if block_id:
            return str(block_id)
    return ""


def is_body_comment(comment: dict) -> bool:
    """只保留可还原到 PRD Markdown 正文的普通正文评论。"""
    if comment.get("parent_type") or comment.get("parent_token"):
        return False
    if (comment.get("relation") or {}).get("content_deleted") is True:
        return False
    return bool(extract_comment_block_id(comment))


def fetch_doc_with_ids(docx_id: str) -> dict | None:
    rc, data, _raw = lark_call(
        [
            "lark-cli",
            "docs",
            "+fetch",
            "--doc",
            docx_id,
            "--api-version",
            "v2",
            "--doc-format",
            "xml",
            "--detail",
            "with-ids",
            "--format",
            "json",
        ]
    )
    if rc != 0 or not data or data.get("ok") is False:
        return None
    return data


def build_block_text_index(with_ids: dict) -> dict[str, dict[str, str]]:
    content = ((with_ids.get("data") or {}).get("document") or {}).get("content") or ""
    if not content:
        return {}
    try:
        root = ET.fromstring(f"<root>{content}</root>")
    except ET.ParseError:
        return {}

    index: dict[str, dict[str, str]] = {}
    for element in root.iter():
        block_id = element.attrib.get("id")
        if not block_id:
            continue
        text = normalize_text("".join(element.itertext()))
        if text:
            index[block_id] = {"text": text, "tag": element.tag}
    return index


def fetch_comments(docx_id: str) -> list[dict] | None:
    comments: list[dict] = []
    page_token = ""
    while True:
        params: dict[str, object] = {
            "file_token": docx_id,
            "file_type": "docx",
            "need_relation": True,
            "page_size": 50,
        }

        if page_token:
            params["page_token"] = page_token

        rc, data, _raw = lark_call(
            [
                "lark-cli",
                "drive",
                "file.comments",
                "list",
                "--params",
                json.dumps(params, ensure_ascii=False),
                "--format",
                "json",
            ]
        )
        if rc != 0 or not data or data.get("ok") is False:
            return None

        payload = data.get("data") or {}
        comments.extend(payload.get("items") or [])
        if not payload.get("has_more"):
            break
        page_token = payload.get("page_token") or ""
        if not page_token:
            break
    return comments


def format_comment_block(comment: dict, index: int) -> str:
    quote = (comment.get("quote") or "").strip()
    replies = (comment.get("reply_list") or {}).get("replies") or []
    lines = ["", f"> [!comment] 评论 {index}"]
    if quote:
        lines.append(f"> - 引用原文：{quote}")
    lines.append("> - 对话内容：")

    if not replies:
        lines.append(">   - 未返回对话内容")
    for reply_index, reply in enumerate(replies, start=1):
        text = extract_reply_text(reply) or "（空）"
        lines.append(f">   - 回复 {reply_index}")
        for line in text.splitlines() or [""]:
            lines.append(f">     {line}")
    lines.append("")
    return "\n".join(lines)


def find_anchor_line(
    lines: list[str], comment: dict, block_info: dict[str, str]
) -> int | None:
    block_text = block_info.get("text", "")
    block_tag = block_info.get("tag", "")
    quote = normalize_text((comment.get("quote") or "").strip())

    if block_tag in {"h1", "h2", "h3", "h4", "h5", "h6"} and block_text:
        for index, line in enumerate(lines):
            line_text = normalize_body_line(line.lstrip("#").strip())
            if line.lstrip().startswith("#") and line_text == block_text:
                return index

    candidates: list[str] = []
    if quote:
        candidates.append(quote)
        if "@" in quote:
            candidates.append(quote.split("@", 1)[0])
    if block_text:
        candidates.append(normalize_text(block_text))
    candidates = [candidate for candidate in candidates if len(candidate) >= 2]

    for candidate in candidates:
        probes = [candidate]
        if len(candidate) > 60:
            probes.extend(
                candidate[start : start + 40]
                for start in range(0, min(len(candidate), 160), 40)
            )
        for probe in probes:
            probe = probe.strip()
            if len(probe) < 2:
                continue
            for index, line in enumerate(lines):
                if probe in normalize_body_line(line):
                    return index
    return None


def insert_comments_at_body_positions(
    content: str,
    comments: list[dict],
    block_text_by_id: dict[str, dict[str, str]],
) -> tuple[str, list[dict]]:
    lines = content.splitlines()
    insertions: dict[int, list[str]] = {}
    unplaced: list[dict] = []

    for index, comment in enumerate(comments, start=1):
        block_id = extract_comment_block_id(comment)
        block_info = block_text_by_id.get(block_id, {})
        anchor = find_anchor_line(lines, comment, block_info)
        if anchor is None:
            unplaced.append(comment)
            continue
        insertions.setdefault(anchor, []).append(format_comment_block(comment, index))

    output: list[str] = []
    for line_index, line in enumerate(lines):
        output.append(line)
        output.extend(insertions.get(line_index, []))

    return "\n".join(output).rstrip() + "\n", unplaced


def insert_body_comments(
    work_dir: Path,
    content: str,
    docx_id: str | None,
    debug_artifacts: bool = False,
) -> tuple[str, dict[str, int | bool]]:
    stats: dict[str, int | bool] = {
        "enabled": bool(docx_id),
        "raw": 0,
        "body": 0,
        "filtered": 0,
        "unplaced": 0,
    }
    if not docx_id:
        return content, stats

    with_ids = fetch_doc_with_ids(docx_id)
    comments = fetch_comments(docx_id)
    if with_ids is None or comments is None:
        return content, stats

    body_comments = [comment for comment in comments if is_body_comment(comment)]

    block_text_by_id = build_block_text_index(with_ids)
    content_with_comments, unplaced = insert_comments_at_body_positions(
        content,
        body_comments,
        block_text_by_id,
    )
    if debug_artifacts:
        (work_dir / "prd-with-ids.json").write_text(
            json.dumps(with_ids, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (work_dir / "prd-comments.json").write_text(
            json.dumps(comments, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (work_dir / "prd-body-comments.json").write_text(
            json.dumps(body_comments, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (work_dir / "prd-unplaced-comments.json").write_text(
            json.dumps(unplaced, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    stats.update(
        {
            "raw": len(comments),
            "body": len(body_comments),
            "filtered": len(comments) - len(body_comments),
            "unplaced": len(unplaced),
        }
    )
    return content_with_comments, stats


# ---------- main ----------


def extract_host(text: str) -> str:
    """从 PRD 文本第一个 lark host URL 提取 host，作为 cite 展开的兜底。"""
    m = re.search(r'(https?://[^/\s"\']+\.(?:feishu|larkoffice|lark)\.[a-z.]+)', text)
    if m:
        return m.group(1).rstrip("/")
    return "https://bytedance.larkoffice.com"


def load_docx_id(work_dir: Path) -> str | None:
    raw_path = work_dir / "prd-raw.json"
    if not raw_path.exists():
        return None
    try:
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return ((raw.get("data") or {}).get("document") or {}).get("document_id")


def main(work_dir: str, debug_artifacts: bool = False) -> int:
    wd = Path(work_dir)
    prd_path = wd / "prd.md"
    if not prd_path.exists():
        print(f"ERROR: missing input {prd_path}", file=sys.stderr)
        return 1

    original = prd_path.read_text(encoding="utf-8")
    if debug_artifacts:
        (wd / "prd.md.before-expand").write_text(original, encoding="utf-8")
    host = extract_host(original)
    docx_id = load_docx_id(wd)

    failed_log = wd / "embeds-failed.jsonl"
    # 每次跑重置 failed log（保持幂等）
    if debug_artifacts and failed_log.exists():
        failed_log.unlink()

    stats = {"expanded": 0, "failed": 0, "by_kind": {}}

    # 4 类占位扫描前先收集 base_refer 已声明的 (token, table_id)，
    # 用来给后面 block_type=53 兜底去重——飞书新版 fetch markdown 已经会渲染 base_refer 占位，
    # 兜底无脑 append 会和主路径产生重复。
    existing_bitable_keys: set[tuple[str, str]] = set()
    for m in EMBED_PATTERNS["base_refer"].finditer(original):
        a = parse_attrs(m.group(1))
        tk = a.get("token")
        tid = a.get("table-id") or a.get("table_id")
        if tk and tid:
            existing_bitable_keys.add((tk, tid))

    def record_fail(kind: str, attrs: dict, reason: str) -> None:
        stats["failed"] += 1
        stats["by_kind"].setdefault(kind, {"ok": 0, "fail": 0})["fail"] += 1
        if debug_artifacts:
            with failed_log.open("a", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        {"kind": kind, "attrs": attrs, "reason": reason},
                        ensure_ascii=False,
                    )
                    + "\n"
                )

    def record_ok(kind: str) -> None:
        stats["expanded"] += 1
        stats["by_kind"].setdefault(kind, {"ok": 0, "fail": 0})["ok"] += 1

    new_text = original
    for kind, pattern in EMBED_PATTERNS.items():

        def replace_one(m: re.Match, _kind: str = kind) -> str:
            attrs = parse_attrs(m.group(1))
            if _kind == "base_refer":
                md, err = expand_bitable(attrs)
            elif _kind == "sheet":
                md, err = expand_sheet(attrs)
            elif _kind == "cite":
                md, err = expand_cite(attrs, host)
            elif _kind == "whiteboard":
                md, err = expand_whiteboard(attrs)
            else:
                md, err = "", f"unknown-kind: {_kind}"
            if err:
                record_fail(_kind, attrs, err)
                return m.group(0)  # 保留原占位标签
            record_ok(_kind)
            return md

        new_text = pattern.sub(replace_one, new_text)

    # block_type=53 兜底：docs +fetch 看不到的嵌入式多维表格视图。
    # 只在传了 docx_id 时启用；典型耗时 ~2s（1 个表 21 条记录）。
    # 去重：跳过主路径 base_refer 占位已渲染过的同一张表，防止重复 append。
    if docx_id:
        bitable_refs = discover_embedded_bitables_via_blocks(docx_id)
        bitable_refs = [
            r
            for r in bitable_refs
            if (r.get("token"), r.get("table-id")) not in existing_bitable_keys
        ]
        if bitable_refs:
            appended_md: list[str] = ["", "", "# 附：嵌入式多维表格", ""]
            for i, attrs in enumerate(bitable_refs, 1):
                md, err = expand_bitable(attrs)
                kind = "embedded_bitable_53"
                if err:
                    record_fail(kind, attrs, err)
                    appended_md.append(f"## 嵌入表格 #{i}（拉取失败：{err}）\n")
                    continue
                record_ok(kind)
                appended_md.append(
                    f"## 嵌入表格 #{i}（app_token={attrs.get('token')}, view_id={attrs.get('view-id')}）\n"
                )
                appended_md.append(md)
                appended_md.append("")
            new_text = new_text.rstrip() + "\n" + "\n".join(appended_md).rstrip() + "\n"

    new_text, comment_stats = insert_body_comments(
        wd,
        new_text,
        docx_id,
        debug_artifacts=debug_artifacts,
    )

    prd_path.write_text(new_text, encoding="utf-8")
    print(
        f"EXPANDED={stats['expanded']} FAILED={stats['failed']} "
        f"BY_KIND={json.dumps(stats['by_kind'], ensure_ascii=False)} "
        f"COMMENTS={json.dumps(comment_stats, ensure_ascii=False)}"
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="飞书嵌入式占位 → markdown 就地展开")
    parser.add_argument("work_dir", help="工作目录（含 prd.md）")
    parser.add_argument(
        "--debug-artifacts",
        action="store_true",
        help="保留 prd.md.before-expand、评论 JSON 和 embeds-failed.jsonl 等调试中间文件",
    )
    args = parser.parse_args()
    sys.exit(main(args.work_dir, debug_artifacts=args.debug_artifacts))
