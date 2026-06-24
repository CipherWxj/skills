#!/usr/bin/env python3
"""飞书嵌入式占位展开：把 prd.md 里 4 类占位调对应 lark-cli 拼成 markdown 就地替换；
传 --docx-id 时再用 docx blocks API 扫一遍 block_type=53（docs +fetch 静默丢失的嵌入式
多维表格视图），append 到 prd.md 末尾「附：嵌入式多维表格」段。

用法：python3 expand_embeds.py <WORK_DIR> [--docx-id <docx_id>]
失败保留原占位、不阻断主流程；明细写 $WORK_DIR/embeds-failed.jsonl。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

LARK_TIMEOUT_SEC = 20
CITE_TRUNCATE = 1500    # cite 只展一层；1500 字够覆盖"为什么链过来"的语境，避免 token 爆炸
BITABLE_LIMIT = 200     # 当前 PRD 多维表格典型 20-50 条，10× safety margin
BLOCK_TYPE_EMBEDDED_BITABLE = 53  # docx v1 blocks API 中嵌入式多维表格视图块

# 4 类占位标签的正则（CDATA 自闭合 + 普通闭合两种 lark 都吐过）
EMBED_PATTERNS = {
    "base_refer": re.compile(r'<base_refer\s+([^>]*?)(?:></base_refer>|/>)'),
    "sheet":      re.compile(r'<sheet\s+([^>]*?)(?:></sheet>|/>)'),
    "cite":       re.compile(r'<cite\s+([^>]*?)(?:></cite>|/>)'),
    "whiteboard": re.compile(r'<whiteboard\s+([^>]*?)(?:></whiteboard>|/>)'),
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


# ---------- 4 类 handler ----------

def expand_bitable(attrs: dict[str, str]) -> tuple[str, str | None]:
    """多维表格 → markdown 表（CLI 直接吐 markdown）。"""
    token = attrs.get("token")
    table_id = attrs.get("table-id") or attrs.get("table_id")
    view_id = attrs.get("view-id") or attrs.get("view_id")
    if not (token and table_id):
        return "", f"missing-attrs token={token!r} table-id={table_id!r}"

    args = [
        "lark-cli", "base", "+record-list",
        "--base-token", token,
        "--table-id", table_id,
        "--limit", str(BITABLE_LIMIT),
        "--format", "markdown",
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

    rc, d, raw = lark_call([
        "lark-cli", "sheets", "+read",
        "--spreadsheet-token", token,
        "--range", f"{sheet_id}!A1:Z1000",
    ])
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
        cols - 1 >= len(row) or row[cols - 1] in (None, "")
        for row in values
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

    url = token if token.startswith(("http://", "https://")) else f"{host}/{file_type}/{token}"
    rc, d, raw = lark_call([
        "lark-cli", "docs", "+fetch",
        "--doc", url,
        "--api-version", "v2",
        "--doc-format", "markdown",
        "--detail", "simple",
    ])
    if d and not d.get("ok"):
        if (d.get("error") or {}).get("code") == ERR_NO_PERMISSION:
            return f"_（引用文档「{title}」：无权限访问）_", None
        return "", f"api-fail: {(d.get('error') or {}).get('message', 'unknown')}"
    if rc != 0 or not d:
        return "", f"cli-fail rc={rc}: {raw[:200]!r}"

    content = (d.get("data") or {}).get("document", {}).get("content") or ""
    header = f"_引用文档「{title}」（{file_type}/{token}）摘要：_\n\n"
    if len(content) > CITE_TRUNCATE:
        return header + content[:CITE_TRUNCATE] + f"\n\n_（已截前 {CITE_TRUNCATE} 字）_", None
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
    rc, d, _raw = lark_call([
        "lark-cli", "api", "GET",
        f"/open-apis/docx/v1/documents/{docx_id}/blocks",
        "--page-size", "500",
    ])
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
        out.append({
            "token": app_token,
            "table-id": table_id,
            "view-id": ref.get("view_id") or "",
        })
    return out


# ---------- main ----------

def extract_host(text: str) -> str:
    """从 PRD 文本第一个 lark host URL 提取 host，作为 cite 展开的兜底。"""
    m = re.search(r'(https?://[^/\s"\']+\.(?:feishu|larkoffice|lark)\.[a-z.]+)', text)
    if m:
        return m.group(1).rstrip("/")
    return "https://bytedance.larkoffice.com"


def main(work_dir: str, docx_id: str | None = None) -> int:
    wd = Path(work_dir)
    prd_path = wd / "prd.md"
    if not prd_path.exists():
        print(f"ERROR: missing input {prd_path}", file=sys.stderr)
        return 1

    original = prd_path.read_text(encoding="utf-8")
    (wd / "prd.md.before-expand").write_text(original, encoding="utf-8")
    host = extract_host(original)

    failed_log = wd / "embeds-failed.jsonl"
    # 每次跑重置 failed log（保持幂等）
    if failed_log.exists():
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
        with failed_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(
                {"kind": kind, "attrs": attrs, "reason": reason},
                ensure_ascii=False,
            ) + "\n")

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
            r for r in bitable_refs
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
                appended_md.append(f"## 嵌入表格 #{i}（app_token={attrs.get('token')}, view_id={attrs.get('view-id')}）\n")
                appended_md.append(md)
                appended_md.append("")
            new_text = new_text.rstrip() + "\n" + "\n".join(appended_md).rstrip() + "\n"

    prd_path.write_text(new_text, encoding="utf-8")
    print(f"EXPANDED={stats['expanded']} FAILED={stats['failed']} BY_KIND={json.dumps(stats['by_kind'], ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="飞书嵌入式占位 → markdown 就地展开")
    parser.add_argument("work_dir", help="工作目录（含 prd.md）")
    parser.add_argument(
        "--docx-id",
        default=None,
        help="飞书 docx document_id（拿到时启用 block_type=53 兜底扫描）",
    )
    args = parser.parse_args()
    sys.exit(main(args.work_dir, args.docx_id))
