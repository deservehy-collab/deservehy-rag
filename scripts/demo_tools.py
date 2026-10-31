import argparse
import json

import httpx

CLIENT_ID = "demo-tools"
CASES = (
    {"label": "订单物流", "question": "订单 1001 的物流到哪了？", "tool": "query_logistics", "ok": True,
     "needs_mcp": True, "must": ("1001",), "any": ("顺丰", "上海分拨中心", "运输中")},
    {"label": "FAQ 命中", "question": "退货政策是什么？", "tool": "query_faq", "ok": True,
     "must": ("退货",), "any": ("7 天", "7天", "七天")},
    {"label": "知识库检索运费", "question": "邮费是多少？", "tool": "query_faq", "ok": True,
     "must": (), "any": ("邮费", "运费")},
    {"label": "型号兼容", "question": "G500 杯盖能装在 B750 上吗？", "tool": "query_faq", "ok": True,
     "must": (), "any": ("G500", "B750")},
    {"label": "知识库拒答", "question": "你们支持货到付款吗？", "tool": "query_faq", "ok": False,
     "must": (), "any": ("没有找到可靠依据", "人工")},
)


class DemoFailure(Exception):
    pass


def ask(client: httpx.Client, message: str) -> dict:
    answer, events, conversation_id, finished = "", [], None, False
    event, data = "", []
    body = {"message": message, "history": [], "client_id": CLIENT_ID}
    with client.stream("POST", "/api/chat", json=body) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].lstrip())
            elif not line and data:
                payload = json.loads("\n".join(data))
                if event == "meta":
                    conversation_id = payload["conversation_id"]
                    events.append("meta")
                elif event == "status":
                    if payload["phase"] == "start":
                        events.append(f"status({payload['tool']} 开始)")
                    else:
                        events.append(f"status({payload['tool']} {'成功' if payload['ok'] else '失败'})")
                elif event == "token":
                    answer += payload["content"]
                elif event == "error":
                    raise DemoFailure(payload["message"])
                elif event == "done":
                    events.append(f"done({payload.get('finish_reason')})")
                    finished = payload.get("finish_reason") == "stop"
                event, data = "", []
    if not finished or not answer:
        raise DemoFailure("对话没有完整结束。")
    return {"conversation_id": conversation_id, "answer": answer, "events": events}


def check(client: httpx.Client, case: dict) -> None:
    hint = "（这条用例的工具由 MCP Server 提供，先双击 start-mcp.bat）" if case.get("needs_mcp") else ""
    outcome = ask(client, case["question"])
    tool_states = [event for event in outcome["events"] if case["tool"] in event]
    if not tool_states:
        raise DemoFailure(f"{case['label']}：模型没有调用 {case['tool']}{hint}。事件：{' → '.join(outcome['events'])}")
    called_ok = "成功" in tool_states[-1]
    if called_ok != case["ok"]:
        raise DemoFailure(f"{case['label']}：{case['tool']} 结果与预期不符（{tool_states[-1]}）{hint}。"
                          f"回答：{outcome['answer']}")
    missing = [word for word in case["must"] if word not in outcome["answer"]]
    if missing:
        raise DemoFailure(f"{case['label']}：回答没有体现工具结果 {missing}。回答：{outcome['answer']}")
    if case["any"] and not any(word in outcome["answer"] for word in case["any"]):
        raise DemoFailure(f"{case['label']}：回答与工具结果对不上。回答：{outcome['answer']}")
    print(f"[{case['label']}] {' → '.join(outcome['events'])}，会话 {outcome['conversation_id']}")
    print(f"  问：{case['question']}")
    print(f"  答：{outcome['answer']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="验收客服聊天里的工具调用（需先启动本地服务）。")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    try:
        with httpx.Client(base_url=args.base_url, timeout=120) as client:
            for case in CASES:
                if case.get("needs_mcp"):
                    print("提示：订单物流的工具来自 MCP Server，先双击 start-mcp.bat；没起也能跑其余四条。")
                check(client, case)
    except (DemoFailure, httpx.HTTPError, ValueError) as exc:
        message = str(exc) if isinstance(exc, DemoFailure) else "服务不可用或返回格式无效，请检查服务、数据库与上游配置。"
        print("验收未通过：" + message)
        return 1
    print("五项验收通过：订单物流、退货政策、运费检索、型号兼容、知识库拒答。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
