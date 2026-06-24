#!/usr/bin/env python3
"""拉取飞书 PRD 正文和评论，生成 prd.md。

用法：
  python3 fetch_prd_with_comments.py <prd_url> <output_dir> [--include-solved]

产物：
  - prd-raw.json：docs +fetch 原始响应
  - prd-comments.json：评论原始响应归并结果
  - prd.md：Markdown 正文 + 文档评论附录
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def run_json(args: list[str]) -> dict:
    proc = subprocess.run(args, text=True, capture_output=True)
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout).strip()
        raise RuntimeError(f"command failed: {' '.join(args)}\n{msg}")
    text = proc.stdout
    start = text.find("{")
    if start < 0:
        raise RuntimeError(f"command did not return JSON: {' '.join(args)}")
    return json.loads(text[start:])


def extract_reply_text(reply: dict) -> str:
    elements = reply.get("reply_elements") or []
    parts: list[str] = []
    for element in elements:
        if not isinstance(element, dict):
            continue
        if element.get("type") == "text":
            parts.append(str(element.get("text") or ""))
        elif element.get("text"):
            parts.append(str(element["text"]))
    return "".join(parts).strip()


def extract_block_id(comment: dict) -> str:
    relation = comment.get("relation") or {}
    relation_text = relation.get("relation")
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


def fetch_comments(doc_token: str, include_solved: bool) -> list[dict]:
    comments: list[dict] = []
    page_token = ""
    while True:
        params: dict[str, object] = {
            "file_token": doc_token,
            "file_type": "docx",
            "need_relation": True,
            "page_size": 50,
        }
        if not include_solved:
            params["is_solved"] = False
        if page_token:
            params["page_token"] = page_token

        resp = run_json([
            "lark-cli",
            "drive",
            "file.comments",
            "list",
            "--params",
            json.dumps(params, ensure_ascii=False),
            "--format",
            "json",
        ])
        if resp.get("ok") is False:
            error = resp.get("error") or {}
            raise RuntimeError(f"评论拉取失败：{error.get('message') or error}")
        data = resp.get("data") or {}
        comments.extend(data.get("items") or [])
        if not data.get("has_more"):
            break
        page_token = data.get("page_token") or ""
        if not page_token:
            break
    return comments


def comments_to_markdown(comments: list[dict], include_solved: bool) -> str:
    if not comments:
        scope = "评论" if include_solved else "未解决评论"
        return f"\n\n## 附：文档评论\n\n未获取到{scope}。\n"

    lines = ["", "", "## 附：文档评论", ""]
    for index, comment in enumerate(comments, start=1):
        block_id = extract_block_id(comment)
        quote = (comment.get("quote") or "").strip()
        replies = (comment.get("reply_list") or {}).get("replies") or []

        lines.append(f"### 评论 {index}")
        lines.append("")
        if block_id:
            lines.append(f"- 位置 block_id：`{block_id}`")
        elif comment.get("parent_type") or comment.get("parent_token"):
            lines.append(
                f"- 位置：嵌入资源 `{comment.get('parent_type') or ''}` "
                f"`{comment.get('parent_token') or ''}`"
            )
        else:
            lines.append("- 位置：未返回可精确定位信息")
        if quote:
            lines.append(f"- 引用原文：{quote}")
        lines.append(f"- 状态：{'已解决' if comment.get('is_solved') else '未解决'}")
        lines.append("")

        for reply_index, reply in enumerate(replies, start=1):
            text = extract_reply_text(reply)
            if not text:
                continue
            lines.append(f"> 回复 {reply_index}：{text}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("prd_url")
    parser.add_argument("output_dir")
    parser.add_argument("--include-solved", action="store_true", help="包含已解决评论")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    raw = run_json([
        "lark-cli",
        "docs",
        "+fetch",
        "--doc",
        args.prd_url,
        "--api-version",
        "v2",
        "--doc-format",
        "markdown",
        "--detail",
        "simple",
        "--format",
        "json",
    ])
    if raw.get("ok") is False:
        error = raw.get("error") or {}
        raise RuntimeError(f"PRD 正文拉取失败：{error.get('message') or error}")
    (output_dir / "prd-raw.json").write_text(
        json.dumps(raw, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    document = (raw.get("data") or {}).get("document") or {}
    content = (document.get("content") or "").strip()
    doc_token = document.get("document_id") or ""
    if not content or not doc_token:
        raise RuntimeError("无法获取 PRD 内容，请检查链接和共享范围")

    comments = fetch_comments(doc_token, args.include_solved)
    (output_dir / "prd-comments.json").write_text(
        json.dumps(comments, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    md = content + comments_to_markdown(comments, args.include_solved)
    (output_dir / "prd.md").write_text(md, encoding="utf-8")
    print(output_dir / "prd.md")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
