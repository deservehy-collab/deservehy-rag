import asyncio
import time

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import func, select

from app.models import Message, Ticket, ToolAuditLog
from app.prompts import (AGENT_ADDENDUM, TICKET_CANCELLED_TEXT, TICKET_CONFIRM_TEXT, TICKET_CREATED_TEXT,
                         TICKET_FAILED_TEXT, TICKET_TIMEOUT_TEXT)
from app.storage import ConversationStore
from app.workflow.confirm_nodes import _ticket_no
from app.workflow.events import unwrap
from app.workflow.graph import Workflow, WorkflowDeps
from tests.support import FixedClassifier, ScriptedModel, _settings, call, msg_chunk, sqlite_factory

TICKET_ARGS = {"description": "保温杯收到就是漏的，想退货", "ticket_type": "售后"}
TICKET_ROUND = [msg_chunk("", "tool_calls", [call("create_ticket", TICKET_ARGS, "call_1")])]


def _boom(*args, **kwargs):
    raise RuntimeError("数据库写不进去")


def _slow_write(*args, **kwargs):
    time.sleep(0.3)          # 超时只是引擎不再等：executor 线程照跑到底，这就是「结果未知」的来由
    return {"ok": True, "data": {"ticket_no": "T20261031099"}}


def build(factory, model, store=None, overrides=None):
    """route=agent 的直达路径：分类器固定成「其他」，绕开检索与闸门。"""
    deps = WorkflowDeps(model=model, settings=_settings(overrides or {}),
                        store=store or ConversationStore(factory), classifier=FixedClassifier("其他"))
    return Workflow(deps=deps, checkpointer=InMemorySaver())


def drive(workflow, conversation_id, question="帮我建个工单，杯子是漏的"):
    messages = [SystemMessage(content="base system"), HumanMessage(content=question)]

    async def run():
        return [item async for item in workflow.respond(conversation_id, question, messages)]
    return asyncio.run(run())


def resume(workflow, conversation_id, decision):
    async def run():
        return [item async for item in workflow.resume(conversation_id, decision)]
    return asyncio.run(run())


def paused(factory, model, user_id="confirm-1", store=None, overrides=None):
    """跑一轮到挂起，返回 (workflow, conversation_id, 卡片载荷)。"""
    store = store or ConversationStore(factory)
    conversation_id = store.start_turn(user_id=user_id, message="帮我建个工单，杯子是漏的")
    workflow = build(factory, model, store, overrides)
    frames = drive(workflow, conversation_id)
    # 按帧名取卡片：靠 frames[-2] 的位置，少发一帧 done 就会红在八竿子打不着的断言上。
    card = next(payload for name, payload in frames if name == "confirm")
    return workflow, conversation_id, dict(card)


def count(factory, model_class):
    with factory() as session:
        return session.scalar(select(func.count()).select_from(model_class)) or 0


def state_values(workflow, conversation_id):
    async def read():
        return await workflow.get_state(conversation_id)
    return asyncio.run(read()).values


def audit_rows(factory, conversation_id):
    with factory() as session:
        rows = session.scalars(select(ToolAuditLog)
                               .where(ToolAuditLog.conversation_id == conversation_id)
                               .order_by(ToolAuditLog.id))
        return [(row.status, row.tool_name, row.tool_call_id, row.result_summary) for row in rows]


def assistant_rows(factory, conversation_id):
    """只算有正文的 assistant 行：工具轮那条只带 tool_calls、正文是 NULL。"""
    with factory() as session:
        rows = session.scalars(select(Message).where(Message.conversation_id == conversation_id,
                                                     Message.role == "assistant").order_by(Message.id))
        return [row.content for row in rows if row.content is not None]


def test_paused_turn_emits_confirm_then_done_and_creates_nothing():
    with sqlite_factory() as factory:
        store = ConversationStore(factory)
        conversation_id = store.start_turn(user_id="confirm-1", message="帮我建个工单，杯子是漏的")
        workflow = build(factory, ScriptedModel([TICKET_ROUND]))
        frames = drive(workflow, conversation_id)
        # 暂停轮不再发 actions 帧：卡片就是这一步唯一的按钮，避免用户点两遍。
        assert [name for name, _ in frames] == ["status", "status", "token", "confirm", "done"]
        # 前端徽章要靠 code 分清「失败」与「等你点卡片」，只靠 ok=False 会把待确认说成失败。
        assert frames[1][1]["phase"] == "end" and frames[1][1]["ok"] is False
        assert frames[1][1]["code"] == "confirm_required"
        card = dict(next(payload for name, payload in frames if name == "confirm"))
        assert card["tool"] == "create_ticket" and card["label"] == "创建工单"
        assert card["tool_call_id"] == "call_1" and card["turn"] == 1
        assert len(card["pending_id"]) >= 8
        assert {"工单类型", "问题描述", "所属会话"} <= {item["label"] for item in card["fields"]}
        assert "确认后" in card["note"]
        assert count(factory, Ticket) == 0 and count(factory, ToolAuditLog) == 0
        # log 节点本轮跑不到，暂停轮的正文由 agent 节点落库，且只落这一条
        assert assistant_rows(factory, conversation_id) == [TICKET_CONFIRM_TEXT]


def test_graph_is_parked_on_the_confirm_node():
    with sqlite_factory() as factory:
        workflow, conversation_id, card = paused(factory, ScriptedModel([TICKET_ROUND]))

        async def read():
            return await workflow.get_state(conversation_id)
        snapshot = asyncio.run(read())
        assert snapshot.next == ("confirm_ticket",)
        assert snapshot.values["pending_write"]["pending_id"] == card["pending_id"]
        assert snapshot.values["stopped_by"] == "await_confirm"
        # 卡片与徽章的中文名必须同一个来源（react 那次调用查到的 spec.label），不许各查各的表。
        assert snapshot.values["pending_write"]["label"] == card["label"] == "创建工单"


def test_accept_executes_once_writes_the_ticket_and_logs_the_reply():
    with sqlite_factory() as factory:
        workflow, conversation_id, card = paused(factory, ScriptedModel([TICKET_ROUND]))
        frames = resume(workflow, conversation_id, "accept")
        assert [name for name, _ in frames] == ["status", "status", "token", "done"]
        assert (frames[0][1]["phase"], frames[0][1]["tool"]) == ("start", "create_ticket")
        assert frames[1][1]["ok"] is True and "工单" in frames[1][1]["summary"]
        text = frames[2][1]["content"]
        with factory() as session:
            ticket = session.scalars(select(Ticket)).one()
            audit = session.scalars(select(ToolAuditLog)).one()
        assert text == TICKET_CREATED_TEXT.format(ticket_no=ticket.ticket_no)
        assert (audit.status, audit.tool_name, audit.tool_call_id) == ("成功", "create_ticket", "call_1")
        assert count(factory, Ticket) == 1
        assert assistant_rows(factory, conversation_id) == [TICKET_CONFIRM_TEXT, text]

        async def read():
            return await workflow.get_state(conversation_id)
        values = asyncio.run(read()).values
        assert values["pending_write"] is None            # 卡片就此作废，重复 resume 打不到第二次


def test_cancel_creates_no_ticket_but_audits_a_denial():
    """验收⑤：点取消 → 工单没建、审计状态「权限拒绝」。"""
    with sqlite_factory() as factory:
        workflow, conversation_id, _ = paused(factory, ScriptedModel([TICKET_ROUND]))
        frames = resume(workflow, conversation_id, "cancel")
        assert [name for name, _ in frames] == ["token", "done"]
        assert frames[0][1]["content"] == TICKET_CANCELLED_TEXT
        assert count(factory, Ticket) == 0
        with factory() as session:
            audit = session.scalars(select(ToolAuditLog)).one()
        assert (audit.status, audit.retry_count, audit.tool_call_id) == ("权限拒绝", 0, "call_1")
        assert audit.arguments == TICKET_ARGS
        assert "取消" in audit.error_message


def test_a_second_resume_is_a_silent_no_op():
    with sqlite_factory() as factory:
        workflow, conversation_id, _ = paused(factory, ScriptedModel([TICKET_ROUND]))
        assert count(factory, Ticket) == 0
        resume(workflow, conversation_id, "accept")
        assert resume(workflow, conversation_id, "accept") == []
        assert count(factory, Ticket) == 1


def test_resuming_a_thread_that_never_parked_is_a_silent_no_op():
    """没挂起过的 thread 收到 Command(resume) 会被 LangGraph 当成新输入从 START 起图，
    而 resume 里没有 question/messages，coref 第一行就 KeyError；护栏必须在 Workflow 自己这边。"""
    with sqlite_factory() as factory:
        store = ConversationStore(factory)
        conversation_id = store.start_turn(user_id="confirm-8", message="随便问问")
        workflow = build(factory, ScriptedModel([]))
        assert resume(workflow, conversation_id, "accept") == []
        assert count(factory, Ticket) == 0
        assert state_values(workflow, conversation_id) == {}   # 图一个节点都没跑


def test_a_failed_write_reports_failure_everywhere_and_voids_the_card():
    """写失败分支：徽章、正文、审计、stopped_by 四个口径都得说「没成」，卡片作废不许再点。"""
    with sqlite_factory() as factory:
        store = ConversationStore(factory)
        store.create_ticket = _boom
        workflow, conversation_id, _ = paused(factory, ScriptedModel([TICKET_ROUND]), store=store)
        frames = resume(workflow, conversation_id, "accept")
        assert [name for name, _ in frames] == ["status", "status", "token", "done"]
        assert frames[1][1]["phase"] == "end" and frames[1][1]["ok"] is False
        assert frames[2][1]["content"] == TICKET_FAILED_TEXT
        # done 帧必须把 stopped_by 带出来：前端就是靠它决定卡片页脚写「已提交。」还是「没提交成功」。
        assert frames[3] == ("done", {"finish_reason": "stop", "stopped_by": "write_failed"})
        assert count(factory, Ticket) == 0
        assert audit_rows(factory, conversation_id)[0][0] == "失败"
        assert state_values(workflow, conversation_id)["stopped_by"] == "write_failed"
        assert resume(workflow, conversation_id, "accept") == []


def test_a_write_timeout_says_result_unknown_and_never_invites_a_retry():
    """写超时不等于没写成：线程还在跑、单可能已经落库，正文必须说「别重复提交」，
    而不是复用「你可以再让我建一次」（评审 I2：那是本章唯一能真造出重复工单的用户路径）。"""
    with sqlite_factory() as factory:
        store = ConversationStore(factory)
        store.create_ticket = _slow_write
        workflow, conversation_id, _ = paused(factory, ScriptedModel([TICKET_ROUND]), store=store,
                                              overrides={"tool_timeout_seconds": 0.05})
        frames = resume(workflow, conversation_id, "accept")
        assert [name for name, _ in frames] == ["status", "status", "token", "done"]
        assert frames[1][1]["ok"] is False
        assert frames[2][1]["content"] == TICKET_TIMEOUT_TEXT
        assert "再让我建一次" not in frames[2][1]["content"]
        assert frames[3] == ("done", {"finish_reason": "stop", "stopped_by": "write_timeout"})
        assert audit_rows(factory, conversation_id)[0][0] == "超时"
        assert count(factory, Ticket) == 0
        assert state_values(workflow, conversation_id)["stopped_by"] == "write_timeout"


def test_a_write_that_returns_no_ticket_no_is_not_reported_as_success():
    """引擎说成功但结果里没有工单号：绿勾「已创建工单」不许配一句「没能提交成功」。"""
    with sqlite_factory() as factory:
        store = ConversationStore(factory)
        store.create_ticket = lambda *args, **kwargs: None
        workflow, conversation_id, _ = paused(factory, ScriptedModel([TICKET_ROUND]), store=store)
        frames = resume(workflow, conversation_id, "accept")
        assert frames[1][1]["ok"] is False
        assert frames[2][1]["content"] == TICKET_FAILED_TEXT
        assert count(factory, Ticket) == 0


def test_the_executed_result_does_not_add_a_second_tool_row():
    """执行结果有意不再补一条 tool 行：历史只回放 user/assistant 正文（storage._history_filter），
    工单号靠正文进历史；再补一条同 tool_call_id 的 tool 行会让 assistant→tool 从一比一变一比二。"""
    with sqlite_factory() as factory:
        workflow, conversation_id, _ = paused(factory, ScriptedModel([TICKET_ROUND]))
        resume(workflow, conversation_id, "accept")
        with factory() as session:
            rows = session.scalars(select(Message).where(Message.conversation_id == conversation_id)
                                   .order_by(Message.id))
            tool_rows = [row for row in rows if row.role == "tool"]
        assert len(tool_rows) == 1 and tool_rows[0].tool_call_id == "call_1"
        assert "confirm_required" in tool_rows[0].content
        history = ConversationStore(factory).context_messages(conversation_id)
        assert history[-1]["role"] == "assistant" and "工单 T" in history[-1]["content"]


def test_a_new_turn_clears_the_leftover_decision():
    """coref 两条都要复位：只清 pending_write 会把上一轮的 "accept" 留在 checkpoint 里。"""
    with sqlite_factory() as factory:
        workflow, conversation_id, _ = paused(factory,
                                              ScriptedModel([TICKET_ROUND, [msg_chunk("在的。")]]))
        resume(workflow, conversation_id, "accept")
        assert state_values(workflow, conversation_id)["write_decision"] == "accept"
        drive(workflow, conversation_id, question="那你们几点下班？")
        values = state_values(workflow, conversation_id)
        assert values["write_decision"] == "" and values["pending_write"] is None


def test_anything_but_accept_takes_the_cancel_branch():
    """选择器不许因为意外的 resume 值抛 KeyError：写操作宁可不做。"""
    with sqlite_factory() as factory:
        workflow, conversation_id, _ = paused(factory, ScriptedModel([TICKET_ROUND]))
        resume(workflow, conversation_id, "whatever")
        assert count(factory, Ticket) == 0
        assert count(factory, ToolAuditLog) == 1


def test_a_new_question_after_the_card_does_not_resurrect_it():
    """用户无视卡片直接问下一句：新一轮必须作废旧卡片，不许再弹同一张。"""
    with sqlite_factory() as factory:
        store = ConversationStore(factory)
        conversation_id = store.start_turn(user_id="confirm-2", message="帮我建个工单，杯子是漏的")
        model = ScriptedModel([TICKET_ROUND, [msg_chunk("我们客服在线到晚上十点。")]])
        workflow = build(factory, model)
        drive(workflow, conversation_id)
        frames = drive(workflow, conversation_id, question="那你们几点下班？")
        assert "confirm" not in [name for name, _ in frames]
        assert count(factory, Ticket) == 0


def test_a_turn_that_never_reaches_the_agent_also_clears_the_card():
    """投诉 / 闲聊走固定话术、不经过 agent 节点，这条路的复位只能放在 coref 里。"""
    with sqlite_factory() as factory:
        store = ConversationStore(factory)
        conversation_id = store.start_turn(user_id="confirm-3", message="帮我建个工单，杯子是漏的")
        workflow = build(factory, ScriptedModel([TICKET_ROUND]))
        drive(workflow, conversation_id)
        workflow.classifier = FixedClassifier("投诉")
        frames = drive(workflow, conversation_id, question="你们这是什么服务态度，我要投诉")
        assert "confirm" not in [name for name, _ in frames]
        assert [name for name, _ in frames][-1] == "done"

        async def read():
            return await workflow.get_state(conversation_id)
        assert asyncio.run(read()).values["pending_write"] is None


def test_unwrap_translates_custom_updates_and_interrupts():
    from types import SimpleNamespace

    assert unwrap("custom", ("token", {"content": "你好"})) == [("token", {"content": "你好"})]
    assert unwrap("updates", {"agent": {"reply": "你好"}}) == []
    assert unwrap("updates", None) == []
    frames = unwrap("updates", {"__interrupt__": [SimpleNamespace(value={"tool": "create_ticket"})]})
    assert [name for name, _ in frames] == ["confirm", "done"]
    assert frames[0][1] == {"tool": "create_ticket"}
    assert frames[1][1] == {"finish_reason": "stop"}
    # 认不出载荷形状时宁可不给卡片，也不许发一张空白 confirm 给前端。
    assert unwrap("updates", {"__interrupt__": [{"value": {"tool": "create_ticket"}}]}) == [
        ("done", {"finish_reason": "stop"})]


def test_ticket_no_only_comes_out_of_the_expected_shape():
    """MCP 写工具返回什么形状不由我们定：解析不出工单号必须是 None，不能抛 AttributeError
    把 execute_write 炸在半路——那时工单已经写了、pending_write 还没清、用户一个字都收不到。"""
    assert _ticket_no('{"ok": true, "data": {"ticket_no": "T20261031001"}}') == "T20261031001"
    for content in ('"一段纯文本"', "123", "null", "{}", '{"ok": true, "data": []}',
                    '{"ok": true, "data": null}', "not json at all"):
        assert _ticket_no(content) is None


def test_agent_prompt_tells_the_model_to_ask_before_calling_the_write_tool():
    """需求 7 前半段「缺必填就主动追问、不许瞎编」只能靠提示词约束，用文本契约钉住。"""
    for phrase in ("create_ticket", "明确要求", "追问", "不许编造", "售后、投诉、咨询"):
        assert phrase in AGENT_ADDENDUM
