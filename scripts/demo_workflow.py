"""跑 5 条验收场景，打印 SSE 事件序列；node= 轨迹看服务日志。"""

import json
import logging
from contextlib import ExitStack

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

QUESTIONS = [
    "G500 杯盖能装在 B750 上吗",
    "订单 1001 的物流到哪了",
    "我要投诉",
    "你好",
    "订单 1001 是什么状态？包裹现在到哪了？",
]


def main() -> None:
    settings = Settings.from_env()
    with ExitStack() as stack:
        client = stack.enter_context(TestClient(create_app(settings)))
        for index, question in enumerate(QUESTIONS, 1):
            print(f"\n=== {index}. {question}")
            with client.stream("POST", "/api/chat",
                                json={"message": question, "client_id": f"demo-ch05-{index}"}) as response:
                for line in response.iter_lines():
                    if line.startswith("event: "):
                        print(f"  {line}")
                    elif line.startswith("data: "):
                        payload = json.loads(line[6:])
                        summary = payload.get("content") or payload.get("summary") or payload.get("items") or payload
                        print(f"    {str(summary)[:120]}")


if __name__ == "__main__":
    main()
