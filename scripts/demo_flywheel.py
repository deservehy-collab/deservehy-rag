"""ch09 六条验收一次跑完（真机脚本）。

前置：MySQL、Milvus、模型服务、Langfuse 容器全在，项目根 .env 填好 key 且 LANGFUSE_ENABLED=true。
每条判据打 PASS/FAIL，有任何 FAIL 退出码 1——不给「看起来跑过了」的假绿灯。

页面级验收（点开 /admin 看原始说法与召回片段）在 T14 Step 7 的浏览器清单里；这个脚本走进程内
TestClient，不占端口。想一边看页面一边跑，另开一个终端起 uvicorn，脚本这边照常跑。

判据只用结构性的东西（走没走拒答话术、有没有引用、池里有没有行、队列里有没有行、token 非零），
不用「回复里必须出现某个词」——模型措辞不可控，那样只会逼着人调 prompt 凑演示。

验收① 的回读走 Observations v2 + `sessionId`：本机是 events_only 部署，v1
`GET /api/public/traces` 404（T2 实测），别再改回去。根 observation 那一行顶层就有
`sessionId` / `projectId` / `traceId`（`trace_context` 是 null，别去里面找）；同一会话有多轮，
按 `startTime` 取最新一行。
"""

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
# `python scripts/demo_flywheel.py` 时 sys.path[0] 是 scripts/，import app 会炸；
# 补上仓库根，使这条命令与 `python -m scripts.demo_flywheel` 等价（probe_trace 同款）。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.config import Settings  # noqa: E402
from app.database import build_database_url, build_engine, build_session_factory  # noqa: E402
from app.flywheel.pipeline import drain  # noqa: E402
from app.knowledge.evalstore import load_runs  # noqa: E402
from app.knowledge.quality import REFUSAL_TEXT  # noqa: E402
from app.main import create_app  # noqa: E402
from app.model import build_model  # noqa: E402
from app.models import LowConfidenceQuestion, ReviewQueue  # noqa: E402
from app.observability.cost_report import _window, collect, render_markdown  # noqa: E402
from scripts.report_eval_trend import render  # noqa: E402

CASES = ("trace", "unknown", "feedback", "cost", "eval")

# 演示题必须是真的「知识库没这块」：保温杯问题在库里有使用说明（rerank 0.16），
# 闸门会放行、模型带证据谨慎作答，根本不会入池。冰箱贴离线探测 top1=0.002、无 evidence，
# 但 fused 候选仍有 5 条快照 —— 正好同时覆盖「低置信」与「入池带召回快照」两条判据。
UNKNOWN_QUESTION = "你们店有卖冰箱贴吗"
APPROVE_ANSWER = (
    "有的，本店有保温杯主题冰箱贴，单价 19.9 元，下单后 48 小时内发货。"
    "款式以商品详情页为准，暂时不支持定制图案。"
)
FEEDBACK_QUESTION = "满多少包邮"

ROOT_OBSERVATION_NAME = "chat-turn"
ROOT_OBSERVATION_FIELDS = "core,basic,metadata,trace_context"


def demo_client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))


def run_turn(client: TestClient, question: str, *, client_id: str = "demo-ch09-01") -> dict:
    """跑一轮真实对话，把 SSE 帧收成结构化结果。probe_trace.py 也复用这个函数。"""
    result = {"conversation_id": None, "message_id": None, "events": [],
              "text": "", "citations": [], "error": None}
    event = None
    with client.stream("POST", "/api/chat",
                       json={"message": question, "client_id": client_id}) as response:
        for line in response.iter_lines():
            if line.startswith("event: "):
                event = line[7:]
                continue
            if not line.startswith("data: "):
                continue
            payload = json.loads(line[6:])
            result["events"].append(event)
            if event == "meta":
                result["conversation_id"] = payload.get("conversation_id")
            elif event == "token":
                result["text"] += payload.get("content") or ""
            elif event == "citations":
                result["citations"] = payload.get("items") or []
            elif event == "done":
                result["message_id"] = payload.get("message_id")
            elif event == "error":
                result["error"] = payload.get("message") or payload.get("code")
            event = None
    return result


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))
    return bool(ok)


def wait_for(fetch, *, timeout: int = 90, interval: int = 5):
    """飞轮是后台任务，池里有了不等于队列里有了；轮询到超时为止，超时再取最后一次。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = fetch()
        if value:
            return value
        time.sleep(interval)
    return fetch()


def pool_row(factory, question: str, source: str) -> dict | None:
    with factory() as session:
        row = session.scalar(
            select(LowConfidenceQuestion)
            .where(LowConfidenceQuestion.raw_question == question,
                   LowConfidenceQuestion.source == source)
            .order_by(LowConfidenceQuestion.id.desc())
        )
        # 只取标量再返回：ORM 对象出了 session 就是 detached，别拿它当返回值用。
        return None if row is None else {
            "id": row.id, "chunks": row.retrieved_chunks or [],
            "matched_review_id": row.matched_review_id,
        }


def matched_review(factory, pool_id: int) -> dict | None:
    with factory() as session:
        review_id = session.scalar(
            select(LowConfidenceQuestion.matched_review_id)
            .where(LowConfidenceQuestion.id == pool_id)
        )
        if review_id is None:
            return None
        row = session.get(ReviewQueue, review_id)
        return {"id": row.id, "normalized_question": row.normalized_question,
                "ai_suggested_answer": row.ai_suggested_answer,
                "occurrence_count": row.occurrence_count, "review_status": row.review_status}


def force_drain(settings, factory, model) -> None:
    print("  自动流水线还没排到，手动 drain 一次。")
    report = asyncio.run(drain(factory, model, limit=settings.flywheel_batch_size,
                               batch_size=settings.flywheel_batch_size,
                               candidates=settings.flywheel_dedupe_candidates))
    print("  drain：", report.as_dict())


def root_observation_of(settings: Settings, session_id: str, *,
                        from_start_time: datetime) -> dict | None:
    """按 sessionId 回读本轮根 observation 行（v2；v1 traces 在 events_only 下 404）。

    `fromStartTime` 把窗口卡在本轮开始之前：同一会话可能有多轮，按 `startTime` 取最新一行。
    `traceId` 与 `projectId` 都在这行上，所以不再单独请求 `/api/public/projects`。
    """
    from scripts.probe_trace import api_get

    if not (settings.langfuse_enabled and settings.langfuse_public_key):
        return None
    try:
        rows = api_get(
            settings.langfuse_host, settings.langfuse_public_key, settings.langfuse_secret_key,
            f"/api/public/v2/observations?name={ROOT_OBSERVATION_NAME}&isRootObservation=true"
            f"&sessionId={session_id}&fields={ROOT_OBSERVATION_FIELDS}&limit=10"
            f"&fromStartTime={quote(from_start_time.isoformat(), safe='')}",
        ).get("data") or []
    except Exception as error:
        print("  trace 查询失败：", type(error).__name__, error)
        return None
    rows = [row for row in rows
            if row.get("traceId") and row.get("sessionId") == session_id]
    if not rows:
        return None
    return max(rows, key=lambda row: str(row.get("startTime") or ""))


def case_trace(client, settings) -> list[bool]:
    print("\n=== 验收① Langfuse 里能看到一整条链路")
    started_at = datetime.now(timezone.utc) - timedelta(seconds=60)
    turn = run_turn(client, "订单 1001 的物流到哪了", client_id="demo-ch09-trace")
    if not check("对话跑通", turn["error"] is None, turn["error"] or ""):
        return [False]
    session_id = f"conv-{turn['conversation_id']}"
    # 上报是后台批量的：先催一次 flush，别让「还没导完」被读成「没上报」。
    from app.observability.tracing import flush_tracing

    flush_tracing()
    print(f"  Langfuse Session：{session_id}")
    print(f"  打开 {settings.langfuse_host} → Sessions 搜 {session_id}："
          "coref/intent/route/retrieve/gate/agent/log 每个节点、每次模型调用的 prompt、"
          "工具入参出参、token 用量与耗时都该在一条 trace 里。")
    root = wait_for(lambda: root_observation_of(settings, session_id,
                                                from_start_time=started_at),
                    timeout=90, interval=10)
    # 上报是后台批量的，没查到不等于没上报；这条 FAIL 先去看容器和 key，别改判据。
    checks = [check("trace 已上报到 Langfuse（v2 根 observation 可读）", root is not None,
                    f"trace_id={(root or {}).get('traceId')}")]
    if root:
        from scripts.probe_trace import _intent_of

        intent, confidence = _intent_of(root)
        print(f"  根 observation：startTime={root.get('startTime')} "
              f"metadata.intent={intent!r} intent_confidence={confidence!r}")
        # URL 形状实测（2026-11-02）：{host}/project/<projectId>/traces/<traceId>，
        # projectId 就是根行上的值（本机 = 项目 id，如 xiaolin-cs）。
        print(f"  trace URL：{settings.langfuse_host}/project/{root.get('projectId')}"
              f"/traces/{root.get('traceId')}")
    return checks


def case_feedback(client, settings, factory, model) -> list[bool]:
    print("\n=== 验收④ 点 👎 → 入池 → 待审队列")
    turn = run_turn(client, FEEDBACK_QUESTION, client_id="demo-ch09-feedback")
    checks = [check("对话跑通并拿到 message_id", bool(turn["message_id"]),
                    f"message_id={turn['message_id']} error={turn['error']}")]
    if not turn["message_id"]:
        return checks
    reply = client.post("/api/feedback", json={
        "conversation_id": turn["conversation_id"], "client_id": "demo-ch09-feedback",
        "message_id": turn["message_id"], "value": "down"})
    checks.append(check("👎 已受理",
                        reply.status_code == 200 and reply.json().get("status") == "recorded",
                        f"{reply.status_code} {reply.text[:100]}"))
    row = wait_for(lambda: pool_row(factory, FEEDBACK_QUESTION, "user_feedback"))
    checks.append(check("按 user_feedback 入池", row is not None))
    if row is None:
        return checks
    if not row["chunks"]:
        print("  这条没带回召回快照：那轮确实没走检索（闲聊/业务数据类），留空是对的，不是 bug。")
    review = wait_for(lambda: matched_review(factory, row["id"]), timeout=30, interval=5)
    if review is None:
        force_drain(settings, factory, model)
        review = matched_review(factory, row["id"])
    checks.append(check("已进待审队列", review is not None,
                        (review or {}).get("normalized_question", "")))
    return checks


def case_unknown(client, settings, factory, model, *, approve: bool) -> list[bool]:
    print("\n=== 验收②③ 答不上就入池带快照，审核通过后同一个问题能答上")
    first = run_turn(client, UNKNOWN_QUESTION, client_id="demo-ch09-unknown")
    checks = [check("走了拒答话术", REFUSAL_TEXT in first["text"], first["text"][:60])]

    row = wait_for(lambda: pool_row(factory, UNKNOWN_QUESTION, "retrieval_low_conf"))
    checks.append(check("问题已入池", row is not None))
    if row is None:
        return checks
    checks.append(check("入池带召回快照", bool(row["chunks"]), f"{len(row['chunks'])} 条"))

    review = wait_for(lambda: matched_review(factory, row["id"]), timeout=30, interval=5)
    if review is None:
        force_drain(settings, factory, model)
        review = matched_review(factory, row["id"])
    checks.append(check("已进待审队列", review is not None))
    if review is None:
        return checks
    print(f"  review_id={review['id']} 标准问法：{review['normalized_question']}"
          f"（合并 {review['occurrence_count']} 次）")
    print(f"  AI 样例答案：{(review['ai_suggested_answer'] or '')[:80]}")

    detail = client.get(f"/api/admin/review-queue/{review['id']}").json()
    sources = detail.get("sources") or []
    has_chunks = any(item["retrieved_chunks"] for item in sources)
    checks.append(check("审核页能拿到原始说法", any(item["raw_question"] for item in sources),
                        f"{len(sources)} 条来源"))
    checks.append(check("审核页能拿到那轮召回片段", has_chunks,
                        "" if has_chunks else
                        "空 = 知识库确实没这块；分数都很低 = 有内容没检到"))

    if not approve:
        print("  --no-approve：不往知识库写东西，后两条判据跳过。")
        return checks
    passed = client.post(f"/api/admin/review-queue/{review['id']}/approve",
                         json={"answer": APPROVE_ANSWER})
    checks.append(check("审核通过并入库", passed.status_code == 200,
                        f"{passed.status_code} {passed.text[:120]}"))
    print(f"  这条通过是真写库：knowledge_chunks.section_path='review_queue:{review['id']}'，"
          "category=会话问答。想撤回就按这个 section_path 删行 + 重建向量。")

    second = run_turn(client, UNKNOWN_QUESTION, client_id="demo-ch09-known")
    checks.append(check("同一个问题不再拒答", REFUSAL_TEXT not in second["text"],
                        second["text"][:80]))
    checks.append(check("回答带引用", bool(second["citations"]),
                        f"{len(second['citations'])} 条"))
    return checks


def case_cost(settings) -> list[bool]:
    print("\n=== 验收⑤ token 花销按意图汇总")
    rows, mode, note = asyncio.run(collect(settings))
    checks = [check("拿到了按意图的分组数据", bool(rows), f"mode={mode} {note}")]
    if rows:
        since, until = _window(None, None)
        print(render_markdown(rows, since=since, until=until, mode=mode, note=note))
        checks.append(check("合计 token 非零",
                            any(row.total_tokens > 0 for row in rows),
                            f"{sum(row.requests for row in rows)} 次请求"))
    else:
        print("  没数据就是没验收过：先确认 T2 探针的 tag 落上了、这几天真跑过对话，"
              "别用 0 糊过去，也别改判据。")
    return checks


def case_eval(settings, factory, *, run_round: bool) -> list[bool]:
    print("\n=== 验收⑥ 评估流水线跑两轮，指标趋势能对比")
    if run_round:
        from scripts.evaluate_retrieval import run_eval

        print("  跑一轮检索评估（--no-faith，80 题）…")
        run_eval(settings, trigger="手动", no_faith=True, factory=factory)
    rows = load_runs(factory, limit=10)
    trend = render(rows)
    print(trend)
    if len(rows) < 2:
        return [check("eval_runs 里至少两轮", False,
                      f"只有 {len(rows)} 轮；再跑一次 "
                      "`python scripts/evaluate_retrieval.py --no-faith` 或加 --eval-round")]
    arrows = "↑" in trend or "↓" in trend
    return [check("eval_runs 里至少两轮", True, f"{len(rows)} 轮"),
            check("趋势里有涨跌箭头", arrows,
                  "" if arrows else
                  "全 `=` 说明两轮配置一模一样；换一个非判据配置（如检索策略）再跑一轮才看得出对比")]


def report(results: dict[str, list[bool]], report_dir: str) -> int:
    print("\n=== 汇总")
    failed = [name for name, checks in results.items() if not all(checks)]
    for name, checks in results.items():
        print(f"  {'PASS' if name not in failed else 'FAIL'} {name}"
              f"（{sum(checks)}/{len(checks)}）")
    directory = Path(report_dir)
    if not directory.is_absolute():
        directory = ROOT / directory
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = directory / f"ch09-demo-{stamp}.json"
    path.write_text(json.dumps(
        {"generated_at": datetime.now().isoformat(timespec="seconds"),
         "cases": {name: {"checks": len(checks), "passed": sum(checks)}
                   for name, checks in results.items()},
         "failed": failed}, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        shown = path.relative_to(ROOT)
    except ValueError:
        shown = path  # --report-dir 给了仓库外的绝对路径，照原样打
    print(f"报告：{shown}")
    print("页面级验收（审核页展开、👎 按钮、375px 不溢出）按 T14 Step 7 的清单在浏览器里过。")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="ch09 六条验收一次跑完（真机）。")
    parser.add_argument("--case", default="all", choices=("all",) + CASES, help="只跑其中一条")
    parser.add_argument("--no-approve", action="store_true",
                        help="验收②③只到「进待审队列」，不真的往知识库写条目")
    parser.add_argument("--eval-round", action="store_true",
                        help="验收⑥现跑一轮检索评估再出趋势（慢，且要在服务关掉之后跑）")
    parser.add_argument("--report-dir", default="evals/reports")
    args = parser.parse_args()
    selected = CASES if args.case == "all" else (args.case,)

    settings = Settings.from_env()
    engine = build_engine(build_database_url(settings))
    factory = build_session_factory(engine)
    model = build_model(settings)
    results: dict[str, list[bool]] = {}
    try:
        if {"trace", "unknown", "feedback"} & set(selected):
            with demo_client(settings) as client:
                if "trace" in selected:
                    results["验收① Langfuse 链路"] = case_trace(client, settings)
                if "unknown" in selected:
                    results["验收②③ 答不上→审核→答得上"] = case_unknown(
                        client, settings, factory, model, approve=not args.no_approve)
                if "feedback" in selected:
                    results["验收④ 👎 入池"] = case_feedback(client, settings, factory, model)
        # 服务关掉之后才跑评估：本机 6GB 显存装不下两份 bge-reranker-v2-m3。
        if "cost" in selected:
            results["验收⑤ token 按意图"] = case_cost(settings)
        if "eval" in selected:
            results["验收⑥ 评估趋势"] = case_eval(settings, factory, run_round=args.eval_round)
    finally:
        # 自己 build 的 model 两个客户端都要关：只关同步那个会留
        # `RuntimeError: Event loop is closed` 噪音（T4 踩过）。
        try:
            asyncio.run(model.root_async_client.close())
        finally:
            model.root_client.close()
        engine.dispose()
    return report(results, args.report_dir)


if __name__ == "__main__":
    raise SystemExit(main())
