"""真模型跑「说清了才发起写操作、信息不足必须先追问」标注集，防止卡片拿到编出来的参数。"""

import asyncio
import json
import tempfile
from datetime import datetime
from pathlib import Path

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import create_engine, func, select

from app.config import Settings
from app.database import build_session_factory
from app.model import build_model
from app.models import Base, Ticket
from app.schemas import IntentResult
from app.storage import ConversationStore
from app.workflow.graph import Workflow, WorkflowDeps

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "evals/ticket_confirm_cases.jsonl"
REPORT_DIR = ROOT / "evals/reports"
GATE_ASK = 1.0
GATE_INVOKE = 0.90
TYPES = {"售后", "投诉", "咨询"}


class FixedClassifier:
    """路由不是本集的评估对象：钉成「其他」，直连主 Agent。"""

    async def classify(self, *args, **kwargs):
        return IntentResult(intent="其他", confidence=1.0)


def card_fields(card):
    return {item["label"]: item["value"] for item in card.get("fields", [])}


def judge(case, text, card):
    if case["kind"] == "ask":
        if card is not None:
            return False, f"信息不足却推了卡片，描述＝{card_fields(card).get('问题描述', '')!r}"
        if not any(mark in text for mark in ("？", "?")):
            return False, f"没有发问，正文＝{text[:60]!r}"
        if not any(word in text for word in case["must_any"]):
            return False, f"问句没问到点上，缺追问词 {case['must_any']}：{text[:60]!r}"
        return True, "先追问，没编参数"
    if card is None:
        return False, f"信息齐全却没出卡片，正文＝{text[:60]!r}"
    fields = card_fields(card)
    if fields.get("工单类型") not in TYPES:
        return False, f"工单类型不合法：{fields.get('工单类型')!r}"
    description = fields.get("问题描述", "")
    if not any(word in description for word in case["expect_any"]):
        return False, f"问题描述没带出关键事实 {case['expect_any']}：{description!r}"
    return True, f"卡片已出（{fields['工单类型']}｜{description[:28]}）"


async def run_case(settings, model, store, case, index):
    conversation_id = store.start_turn(user_id=f"eval-ticket-{index}", message=case["message"])
    workflow = Workflow(deps=WorkflowDeps(model=model, settings=settings, store=store,
                                          classifier=FixedClassifier()),
                        checkpointer=InMemorySaver())
    frames = []
    async for item in workflow.respond(conversation_id, case["message"],
                                       [HumanMessage(content=case["message"])]):
        frames.append(item)
    text = "".join(payload["content"] for event, payload in frames if event == "token")
    card = next((payload for event, payload in frames if event == "confirm"), None)
    return text, card


async def main() -> None:
    settings = Settings.from_env()
    model = build_model(settings)
    cases = [json.loads(line) for line in CASES.read_text(encoding="utf-8").splitlines() if line.strip()]
    print(f"模型：{settings.openai_model}；样本：{len(cases)}")

    with tempfile.TemporaryDirectory() as workdir:
        engine = create_engine(f"sqlite:///{Path(workdir) / 'eval.db'}")
        Base.metadata.create_all(engine)
        store = ConversationStore(build_session_factory(engine))
        rows = []
        for index, case in enumerate(cases, start=1):
            text, card = await run_case(settings, model, store, case, index)
            ok, reason = judge(case, text, card)
            rows.append((case, ok, reason))
            print(f"- {'OK ' if ok else 'ERR'} `{case['id']}` {case['message']}｜{reason}")
        with store.session_factory() as session:
            tickets = session.scalar(select(func.count()).select_from(Ticket))
        engine.dispose()

    ask = [row for row in rows if row[0]["kind"] == "ask"]
    invoke = [row for row in rows if row[0]["kind"] == "invoke"]
    ask_rate = sum(1 for row in ask if row[1]) / len(ask) if ask else 0.0
    invoke_rate = sum(1 for row in invoke if row[1]) / len(invoke) if invoke else 0.0
    passed = ask_rate >= GATE_ASK and invoke_rate >= GATE_INVOKE and tickets == 0

    lines = [
        "# ch08 建工单确认流评估",
        "",
        f"- 时间：{datetime.now():%Y-%m-%d %H:%M}",
        f"- 模型：{settings.openai_model}",
        f"- 样本：{len(rows)}（ask {len(ask)}／invoke {len(invoke)}）",
        f"- 该追问追问率：{ask_rate:.3f}（门槛 {GATE_ASK:.3f}）",
        f"- 说清了直接出卡片率：{invoke_rate:.3f}（门槛 {GATE_INVOKE:.2f}）",
        f"- 评估过程中误落的工单数：{tickets}（必须为 0：卡片只预览，不提交）",
        f"- 结论：{'通过' if passed else '未达标，改 AGENT_ADDENDUM 的追问要求与示例，不放宽判据'}",
        "",
        "## 逐例结果",
        "",
        *(f"- {'OK ' if ok else 'ERR'} `{case['id']}`（{case['kind']}）{case['message']}｜{reason}"
          for case, ok, reason in rows),
        "",
    ]
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report = REPORT_DIR / f"ch08-ticket-confirm-{datetime.now():%Y%m%d-%H%M%S}.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    print(f"追问率 {ask_rate:.3f}／发起率 {invoke_rate:.3f}；报告：{report}")

    await model.root_async_client.close()
    model.root_client.close()
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
