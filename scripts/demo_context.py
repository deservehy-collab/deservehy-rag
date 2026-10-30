"""ch07 上下文管理演示：同一会话连聊二十多轮，看三层级联、后台摘要与靠梗概答题。

不带参数就按验收的演示配置跑（窗口 18000 → 历史 5650 / 层1 3954 / 层2 1695）；
环境变量里已经给出的键优先，脚本不覆盖。
`--default-window` 改用 .env 的默认窗口，验证 20 轮既不降级也不触发摘要。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from contextlib import ExitStack

from fastapi.testclient import TestClient

from app.config import Settings
from app.context import ContextBudget
from app.main import create_app

# (字段, 演示值, 对应环境变量)；环境变量已设置时以环境变量为准。
DEMO_OVERRIDES = (
    ("context_window_tokens", 18000, "MODEL_CONTEXT_WINDOW"),
    ("max_output_tokens", 2000, "MAX_OUTPUT_TOKENS"),
    ("max_user_input_tokens", 2000, "MAX_USER_INPUT_TOKENS"),
    ("workflow_agent_max_steps", 3, "MAX_AGENT_STEPS"),
    ("tool_result_max_tokens", 1200, "TOOL_RESULT_MAX_TOKENS"),
    ("knowledge_top_k", 5, "RERANK_TOP_K"),
)

FIRST_QUESTION = "订单 1001 的保温杯漏水了，我要退货退款"
LAST_QUESTION = "最开始那个订单后来怎么说？"
# 问题都挑必然给出多条要点的答案：层 1 / 层 2 的预算是按稳态占用算的，
# 寒暄和拒答轮几乎不涨历史，级联要二三十轮才出现，演示得把内容密度做上去。
MIDDLE_QUESTIONS = (
    "订单 1001 现在物流到哪了？",
    "把七天无理由退货的条件、期限和不能退的情况一起讲清楚。",
    "质量问题换货怎么申请，需要准备哪些凭证？",
    "退款到账时间是怎么规定的，原路返回和银行卡有区别吗？",
    "退货运费谁承担，质量问题和不想要有区别吗？",
    "售后申请流程一共几步，每一步要做什么？",
    "订单 1002 什么时候发货？",
    "1002 能改收货地址吗？",
    "配送范围和时效怎么算，偏远地区会慢多久？",
    "运费标准是什么，满多少包邮？",
    "物流异常了怎么处理，要等多久才有结果？",
    "保温杯使用说明里有哪些注意事项，能直接装开水吗？",
    "保温杯保修政策覆盖哪些部件，保修多久？",
    "纺织品洗涤要注意什么，下水之后还能换吗？",
    "家居袜不合适能退吗，要满足什么条件？",
    "保温杯漏水可能是什么原因，能自己排查吗？",
    "小家电保修多久，怎么报修？",
    "联系售后之前要准备哪些信息？",
    "保温杯系列有哪些型号，容量和价格分别是多少？",
    "G500 杯盖能装在 B750 上吗？",
    "常见型号问题里还有哪些容易搞错的？",
    "那 B750 的保修和 G500 一样吗？",
    "帮我把订单 1001 的退货申请提交一下。",
    "这单现在走到哪一步了，接下来我该做什么？",
    "如果审核不通过，我还能怎么办？",
    "这单最后能退多少钱，运费怎么算？",
)

MARKERS = ("layer1 降级", "summary trigger", "summary done")


def build_settings(default_window: bool) -> Settings:
    settings = Settings.from_env()
    if default_window:
        return settings
    updates = {
        field: value
        for field, value, env_name in DEMO_OVERRIDES
        if env_name not in os.environ
    }
    return settings.model_copy(update=updates)


def read_from(path: str, offset: int) -> tuple[str, int]:
    try:
        with open(path, "rb") as handle:
            handle.seek(offset)
            data = handle.read()
            return data.decode("utf-8", "replace"), handle.tell()
    except OSError:
        return "", offset


def last_line(text: str, marker: str) -> str:
    for line in reversed(text.splitlines()):
        if marker in line:
            return line
    return ""


def field(line: str, pattern: str) -> str:
    match = re.search(pattern, line)
    return match.group(1) if match else "-"


def summary_segments(lines: list[str]) -> dict[int, str]:
    """从 history_ctx / model_ctx 的 `summary=['seq=1 …']` 里取回每段梗概正文。"""
    found: dict[int, str] = {}
    for line in lines:
        for seq, body in re.findall(r"'seq=(\d+) (.*?)'", line):
            found.setdefault(int(seq), body)
    return found


def done_batches(lines: list[str]) -> list[dict]:
    batches = []
    for line in lines:
        match = re.search(r"summary done conv=\d+ seq=(\d+) from=(\d+) upto=(\d+)", line)
        if match:
            batches.append({"seq": int(match.group(1)), "from": int(match.group(2)),
                            "upto": int(match.group(3))})
    return batches


def ask(client: TestClient, question: str, client_id: str, conversation_id: int | None):
    body: dict = {"message": question, "client_id": client_id}
    if conversation_id:
        body["conversation_id"] = conversation_id
    frames: list[tuple[str | None, dict]] = []
    with client.stream("POST", "/api/chat", json=body) as response:
        event: str | None = None
        for line in response.iter_lines():
            if line.startswith("event: "):
                event = line[len("event: "):]
            elif line.startswith("data: "):
                frames.append((event, json.loads(line[len("data: "):])))
    answer = "".join(payload.get("content") or "" for name, payload in frames if name == "token")
    turn_id = next((payload["conversation_id"] for name, payload in frames if name == "meta"), None)
    errors = [payload for name, payload in frames if name == "error"]
    return answer, turn_id, errors


def wait_summaries(application, conversation_id: int, timeout: float = 90.0) -> None:
    """等后台摘要排空：梗概必须在本轮之内落库，下一轮才谈得上「靠梗概答题」。

    排空是演示的时序夹具，不是产品行为——线上不阻塞，摘要该晚一轮就晚一轮。
    """
    summary = getattr(application.state.service, "summary", None)
    if summary is None:
        return
    deadline = time.monotonic() + timeout
    while summary.pending and time.monotonic() < deadline:
        time.sleep(0.5)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--default-window", action="store_true",
                        help="用 .env 的默认窗口跑，验证不降级、不摘要")
    parser.add_argument("--turns", type=int, default=len(MIDDLE_QUESTIONS),
                        help=f"中间轮数，默认 {len(MIDDLE_QUESTIONS)}")
    parser.add_argument("--only-final", type=int, default=None, metavar="会话 id",
                        help="跳过铺历史，只对已有会话追问末问（梗概已落库时用它补验，省上游额度）")
    parser.add_argument("--client-id", default="demo-ch07")
    args = parser.parse_args()

    settings = build_settings(args.default_window)
    budget = ContextBudget.from_settings(settings)
    print(f"窗口 {budget.window}｜系统 {budget.system}｜证据 {budget.evidence}｜梗概 {budget.summary}"
          f"｜输出 {budget.output}｜余量 {budget.safety}｜ReAct 峰值 {budget.react_peak}")
    print(f"历史预算 {budget.history}（想留 {budget.wanted} / 匀得出 {budget.available}）"
          f"｜层1 {budget.layer1}｜层2 {budget.layer2}")
    problem = budget.startup_problem()
    if problem:
        print(f"启动自检报警：{problem}")
        return 2

    if args.only_final:
        questions = [LAST_QUESTION]
    else:
        questions = [FIRST_QUESTION, *MIDDLE_QUESTIONS[: args.turns], LAST_QUESTION]
    run_start = os.path.getsize(settings.log_file) if os.path.exists(settings.log_file) else 0
    offset = run_start
    error_frames: list[dict] = []
    conversation_id: int | None = args.only_final
    final_answer = ""
    final_segments = 0
    final_upto = 0

    with ExitStack() as stack:
        application = create_app(settings)
        # TestClient 的 with 才会跑 lifespan（建引擎、起后台摘要）；FastAPI 本身不是上下文管理器。
        client = stack.enter_context(TestClient(application))
        for index, question in enumerate(questions, 1):
            answer, turn_id, errors = ask(client, question, args.client_id, conversation_id)
            conversation_id = conversation_id or turn_id
            error_frames.extend(errors)
            final_answer = answer
            chunk, offset = read_from(settings.log_file, offset)
            # history_ctx 每轮必打（含闲聊兜底轮）；model_ctx 只有主力 Agent 调模型时才有。
            history_line = last_line(chunk, "history_ctx ")
            model_line = last_line(chunk, "model_ctx ")
            final_segments = history_line.count("seq=")
            final_upto = int(field(history_line, r"summary_upto=(\d+)"))
            print(f"\n[{index}/{len(questions)}] {question}")
            print(f"  路由 {field(chunk, r'route=([a-z_]+)')}"
                  f"｜层2 {field(history_line, r'layer2=(\d+)')} 条 / "
                  f"{field(history_line, r'layer2_raw_tokens=(\d+)')} tok（预算 {budget.layer2}）"
                  f"｜层1 {field(model_line, r'layer1=(\d+)')} 条"
                  f"｜梗概 {final_segments} 段 / {field(model_line, r'summary_tokens=(\d+)')} tok"
                  f"｜送模型 {field(model_line, r'total_tokens=(\d+)')} tok")
            print(f"  锚点 summary_upto={field(history_line, r'summary_upto=(\d+)')}"
                  f" layer1_from={field(history_line, r'layer1_from=(\d+)')}")
            for line in chunk.splitlines():
                if any(marker in line for marker in MARKERS):
                    print(f"  级联 {line.split(' INFO ', 1)[-1][:160]}")
            print(f"  答：{answer[:180]}{'…' if len(answer) > 180 else ''}")
            if index < len(questions):
                wait_summaries(application, conversation_id)
        # 出 with 之前把整通会话的行取出来：首问那行的 id 是「只能来自梗概」的证据锚点。
        store_rows = application.state.service.store.conversation_messages(
            conversation_id, args.client_id) or []

    # 关停时 lifespan 已排空后台摘要，这段日志里含本轮完整级联。
    # 按会话号过滤：日志是共享文件，别的服务或测试写进来的行不该算进这次验收。
    run_log, _ = read_from(settings.log_file, run_start)
    own = [line for line in run_log.splitlines()
           if re.search(rf"conv={conversation_id}(?![0-9])", line)]
    counts = {marker: sum(1 for line in own if marker in line) for marker in MARKERS}
    batches = done_batches(own)
    segments = summary_segments(own)
    print("\n=== 本轮日志级联 ===")
    for marker in MARKERS:
        print(f"  {marker}: {counts[marker]} 次")
    for line in own:
        if any(marker in line for marker in MARKERS) or "summary start" in line or "summary skip" in line:
            print(f"  {line.split(' INFO ', 1)[-1][:200]}")
    print("\n=== 梗概分段 ===")
    for batch in batches:
        body = segments.get(batch["seq"], "")
        print(f"  seq={batch['seq']} 覆盖 {batch['from']}→{batch['upto']}｜{len(body)} 字｜{body[:70]}")
    if not batches:
        # --only-final 这一轮不会有 summary done，但上一批留下的段仍挂在上下文里。
        for seq in sorted(segments):
            print(f"  seq={seq}（历史段）｜{len(segments[seq])} 字｜{segments[seq][:70]}")

    failures: list[str] = []
    if args.default_window:
        if counts["layer1 降级"]:
            failures.append(f"默认窗口不该出现 layer1 降级，实际 {counts['layer1 降级']} 次")
        if counts["summary trigger"]:
            failures.append(f"默认窗口不该出现 summary trigger，实际 {counts['summary trigger']} 次")
    elif args.only_final is None:
        for marker in MARKERS:
            if not counts[marker]:
                failures.append(f"缺少 {marker} 日志")
        # 锚点只能落在「真正喂进摘要器的最后一行」上：分段必须首尾相接、不许留空洞。
        cursor = batches[0]["from"] if batches else None
        for batch in batches:
            if batch["from"] != cursor:
                failures.append(f"梗概分段有空洞：应从 {cursor} 接上，实际 {batch}")
            cursor = batch["upto"]
        if not batches:
            failures.append("没有 summary done 分段，无法核对梗概覆盖")
    if error_frames:
        failures.append(f"出现 error 帧：{error_frames}")
    # 「靠梗概答题」要真靠梗概：末问那轮的上下文里必须挂着梗概，且正文里就有这条线索。
    if not args.default_window:
        if not final_segments:
            failures.append("末问那轮梗概 0 段：摘要落库晚于本轮上下文，这条没验到")
        if not segments:
            failures.append("日志里没解析出任何梗概正文，无法核对覆盖内容")
        elif "1001" not in " ".join(segments.values()):
            failures.append("梗概正文里没有订单 1001——末问靠的是层 1 原文，不算靠梗概答题")
        # 机械证据：最早那句话所在的行必须已落在 summary_upto 之前，它就只能从梗概里来。
        first_id = next((row["id"] for row in store_rows if row["content"] == FIRST_QUESTION), None)
        if first_id is None:
            failures.append("库里找不到首问那一行，无法证明末问靠的是梗概")
        elif first_id > final_upto:
            failures.append(f"首问 id={first_id} 尚未被梗概覆盖（summary_upto={final_upto}），末问靠的是原文")
    if "1001" not in final_answer:
        failures.append(f"末问未提到 1001：{final_answer}")
    # 「后来怎么说」要答出退货退款：演示配置里这线索只存在于梗概，靠梗概答题才算通；
    # 默认窗口不压梗概，最早那几轮原文还在层 1 里，能认出 1001 就达到验收③，
    # 至于说没说「退」取决于当轮 Agent 有没有把工具跑完（步数上限会截断答复）。
    if not args.default_window and "退" not in final_answer:
        failures.append(f"末问未提到退货/退款：{final_answer}")

    print("\n=== 结论 ===")
    print(f"  会话 {conversation_id}｜轮数 {len(questions)}｜末问回答 {len(final_answer)} 字")
    if failures:
        for item in failures:
            print(f"  FAIL {item}")
        return 1
    if args.default_window:
        print("  PASS 默认窗口 20 轮：未触发层 1 降级与摘要，末问仍认得最早的订单。")
    else:
        print("  PASS 三层级联、后台摘要与靠梗概答题全部符合预期。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
