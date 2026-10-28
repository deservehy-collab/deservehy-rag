import argparse
import json

import httpx

from app.schemas import AfterSales


class DemoFailure(Exception):
    pass


def chat(client: httpx.Client, message: str, history: list[dict]) -> str:
    answer = ""
    finished = False
    event = ""
    data = []
    with client.stream("POST", "/api/chat", json={"message": message, "history": history}) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].lstrip())
            elif not line and data:
                payload = json.loads("\n".join(data))
                if event == "token":
                    answer += payload["content"]
                    print(payload["content"], end="", flush=True)
                elif event == "error":
                    raise DemoFailure(payload["message"])
                elif event == "done":
                    if payload.get("finish_reason") != "stop":
                        raise DemoFailure("回答被截断，不能算完整验收。")
                    finished = True
                event, data = "", []
    print()
    if not finished or not answer:
        raise DemoFailure("对话没有完整结束。")
    return answer


def run_demo(client: httpx.Client) -> dict:
    first_question = "我的订单号是 A20261028001，收到的杯子碎了，请记住这件事。"
    print("第一轮：", flush=True)
    first_answer = chat(client, first_question, [])
    print("第二轮：", flush=True)
    second_answer = chat(client, "我刚才说的订单号是什么？只回答订单号。", [
        {"role": "user", "content": first_question}, {"role": "assistant", "content": first_answer},
    ])
    if "A20261028001" not in second_answer:
        raise DemoFailure("第二轮没有接住订单号上下文。")
    response = client.post("/api/after-sales/extract", json={"description": "订单 A20261028001 的杯子碎了，我不想换货，请给我退款。"})
    response.raise_for_status()
    extraction = AfterSales.model_validate(response.json()).model_dump()
    if extraction["order_id"] != "A20261028001" or extraction["request_type"] != "refund" or not extraction["expected_solution"]:
        raise DemoFailure("售后提取结果不符合样例预期。")
    print("售后提取：", json.dumps(extraction, ensure_ascii=False))
    return {"first_answer": first_answer, "second_answer": second_answer, "extraction": extraction}


def main() -> int:
    parser = argparse.ArgumentParser(description="对实际运行的客服服务进行两轮对话与售后提取验收。")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    try:
        with httpx.Client(base_url=args.base_url, timeout=90) as client:
            run_demo(client)
    except (DemoFailure, httpx.HTTPError, ValueError) as exc:
        message = str(exc) if isinstance(exc, DemoFailure) else "服务不可用或返回格式无效，请检查服务和上游配置。"
        print("验收未通过：" + message)
        return 1
    print("三项样例验收通过；仅代表当前服务所连接的上游。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
