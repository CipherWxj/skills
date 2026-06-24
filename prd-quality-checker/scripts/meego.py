#!/usr/bin/env python3
"""Meego 工作项 PRD 链接调试脚本。

用途：
    根据 Meego story 工作项 ID 查询工作项详情，并提取需求文档字段（wiki）。

调用示例：
    source .venv/bin/activate
    python3 .trae/skills/prd-quality-checker/scripts/meego.py --meegoid 123456789

输出：
    JSON 对象，包含 prd_url。
"""

import argparse
import json
import time
import urllib.error
import urllib.request


class MeegoTools:
    def __init__(self):
        self.plugin_id = "xxxxxxxxx"  # 需补充插件ID
        self.plugin_secret = (
            "xxxxxxxxxx"  # 需补充插件密钥
        )
        self.host = "https://project.feishu.cn"
        self.project_key = "dcar"
        self.user_key = "xxxxxxxxx"  # 需补充用户key
        self.plugin_token = self.get_plugin_token()["data"]["token"]
        self.headers = {
            "Content-Type": "application/json",
            "X-PLUGIN-TOKEN": self.plugin_token,
            "X-USER-KEY": self.user_key,
        }

    def get_plugin_token(self):
        """获取插件token"""
        self.request_data = {
            "url": f"{self.host}/open_api/authen/plugin_token",
            "body": {
                "plugin_id": self.plugin_id,
                "plugin_secret": self.plugin_secret,
                "type": 0,
            },
            "headers": {
                "Content-Type": "application/json",
            },
            "method": "post",
        }
        return self.run(printed=False)

    def post_request(self, path="", body=None):
        """POST请求"""
        if not path:
            raise ValueError("path is required")
        self.request_data = {
            "url": f"{self.host}{path}",
            "body": body or {},
            "headers": self.headers,
            "method": "post",
        }
        return self.run(printed=False)

    def run(self, printed=True):
        """执行 self.request_data 描述的 HTTP 请求。

        返回请求返回的 response。若响应体是 JSON，则返回解析后的对象；
        否则返回原始文本。
        """
        request_data = getattr(self, "request_data", None)
        if not request_data:
            raise ValueError("request_data is required before calling run()")

        url = request_data.get("url")
        if not url:
            raise ValueError("request_data.url is required")

        method = str(request_data.get("method") or "get").upper()
        body = request_data.get("body") or {}
        headers = dict(request_data.get("headers") or {})
        runtimes = int(request_data.get("runtimes") or 1)

        data = None
        if method != "GET":
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")

        last_error = None
        for index in range(max(runtimes, 1)):
            try:
                request = urllib.request.Request(
                    url=url,
                    data=data,
                    headers=headers,
                    method=method,
                )
                with urllib.request.urlopen(request, timeout=30) as response:
                    response_text = response.read().decode("utf-8")

                if printed:
                    print(response_text)

                return _parse_response(response_text)
            except urllib.error.HTTPError as error:
                response_text = error.read().decode("utf-8", errors="replace")
                if printed:
                    print(response_text)
                return _parse_response(response_text)
            except (urllib.error.URLError, TimeoutError, RuntimeError) as error:
                last_error = error
                if index < max(runtimes, 1) - 1:
                    time.sleep(1)

        raise RuntimeError(
            f"request failed after {max(runtimes, 1)} attempt(s): {last_error}"
        )

    def get_work_item_info(self, work_item_id, work_item_type_key):
        """获取单个飞书项目工作项详情，根据工作项类型（story）。"""
        if work_item_type_key not in ["story"]:
            raise ValueError("暂时仅支持查询 需求文档")

        response = self.post_request(
            path=f"/open_api/{self.project_key}/work_item/{work_item_type_key}/query",
            body={"work_item_ids": [work_item_id]},
        )
        work_items = response.get("data") or []
        if not work_items:
            return {}
        work_item = work_items[0]
        prd_wiki = ""
        for field in work_item.get("fields") or []:
            field_key = field.get("field_key")
            if field_key == "wiki":
                prd_wiki = field.get("field_value") or ""
                break

        return {
            "prd_url": prd_wiki,
        }


def _parse_response(response_text):
    if not response_text:
        return {}
    try:
        return json.loads(response_text)
    except json.JSONDecodeError:
        return response_text


def _parse_work_item_id(value):
    """解析单个工作项 ID。"""
    item = value.strip()
    if not item:
        raise argparse.ArgumentTypeError("work item id 不能为空")
    if "," in item:
        raise argparse.ArgumentTypeError("仅支持单个工作项 ID，不支持多个 ID")
    try:
        return int(item)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"非法工作项 ID: {item}") from error


def main(argv=None):
    """主函数，查询 Meego 工作项信息"""
    parser = argparse.ArgumentParser(description="查询 Meego 工作项信息")
    parser.add_argument(
        "--meegoid",
        dest="work_item_id",
        type=_parse_work_item_id,
        required=True,
        help="单个工作项 ID，例如：123456789",
    )
    args = parser.parse_args(argv)

    tool = MeegoTools()

    result = tool.get_work_item_info(
        work_item_id=args.work_item_id,
        work_item_type_key="story",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
