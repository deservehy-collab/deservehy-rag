"""跑 ch06 三轮演示，打印 SSE 事件摘要；node=/resolved=/policy_ 轨迹看服务日志。"""

import json
import logging
import sys
from contextlib import ExitStack

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

TURNS = [
    ("这个能退吗", "期望 orders 帧 + 订单卡片话术"),
    ("订单 1001", "期望 coref 合并 → query_order → 扩写 → 检索 → 正文 + refund 动作"),
    ("订单 1001 的物流到哪了", "期望回到 agent 路由"),
]


def summarize(event: str, payload: dict):
    if event in {"status", "orders", "actions", "citations", "done", "meta"}:
        if event == "actions":
            return json.dumps({"items": [item["id"] for item in payload.get("items", [])],
                               "refund": payload.get("refund")}, ensure_ascii=False)
        if event == "orders":
            return json.dumps({"reason": payload.get("reason"),
                               "orders": [item["order_id"] for item in payload.get("items", [])]},
                              ensure_ascii=False)
        if event == "citations":
            return f"{len(payload.get('items', []))} 条引用"
        return json.dumps(payload, ensure_ascii=False)[:200]
    return None


def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)
    settings = Settings.from_env()
    with ExitStack() as stack:
        client = stack.enter_context(TestClient(create_app(settings)))
        conversation_id = None
        for index, (question, expectation) in enumerate(TURNS, 1):
            print(f"\n=== 第 {index} 轮：{question}")
            print(f"    （{expectation}）")
            body = {"message": question, "client_id": "demo-ch06-router"}
            if conversation_id:
                body["conversation_id"] = conversation_id
            seen, answer = [], []
            with client.stream("POST", "/api/chat", json=body) as response:
                if response.status_code != 200:
                    raise SystemExit(f"请求失败：{response.status_code}")
                event = None
                for line in response.iter_lines():
                    if line.startswith("event: "):
                        event = line[7:].strip()
                    elif line.startswith("data: ") and event:
                        payload = json.loads(line[6:])
                        seen.append(event)
                        if event == "meta":
                            conversation_id = payload["conversation_id"]
                        if event == "token":
                            answer.append(payload.get("content", ""))
                            continue
                        summary = summarize(event, payload)
                        if summary is not None:
                            print(f"  [{event}] {summary}")
            if "error" in seen:
                raise SystemExit("本轮返回了 error 帧")
            print(f"  [正文] {''.join(answer).strip()[:220]}")
        print(f"\n演示结束，会话 {conversation_id}")


if __name__ == "__main__":
    main()
