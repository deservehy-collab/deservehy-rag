from langchain_core.messages import HumanMessage, SystemMessage

from app.context import ContextBudget, estimate_text_tokens, estimate_tokens
from tests.support import _settings


def test_chinese_counts_one_token_and_ascii_is_discounted():
    assert estimate_text_tokens("退款") == 2
    assert estimate_text_tokens("hello") == 2
    assert estimate_text_tokens("订单1001") == 4
    assert estimate_tokens([SystemMessage(content="退款")]) == 10
    assert estimate_tokens([SystemMessage(content="退款"), HumanMessage(content="hello")]) == 20


def test_demo_budget_matches_acceptance_numbers():
    settings = _settings({
        "context_window_tokens": 18000, "max_output_tokens": 2000,
        "max_user_input_tokens": 2000, "workflow_agent_max_steps": 3,
        "tool_result_max_tokens": 1200, "knowledge_top_k": 5,
        "tool_schema_reserve_tokens": 400,
    })
    budget = ContextBudget.from_settings(settings)
    # ch08/T4 起 `TicketArgs.ticket_type` 的 JSON Schema 带上合法取值名单；ch08/T9 起
    # system 再扣 `tool_schema_reserve_tokens`（默认 400，给 MCP 动态 schema 留位），
    # 同时旧实现把 `OrderArgs` 数了两遍的 bug 被 `builtin_json_schemas()` 修掉（约 −70）。
    assert (budget.history, budget.layer1, budget.layer2) == (5308, 3715, 1592)


def test_default_budget_leaves_twenty_rounds_room():
    budget = ContextBudget.from_settings(_settings({}))
    assert budget.history == 12000
    assert (budget.layer1, budget.layer2) == (8399, 3600)
    assert budget.startup_problem() is None


def test_tiny_window_reports_startup_problem():
    budget = ContextBudget.from_settings(_settings({"context_window_tokens": 2048}))
    assert budget.history == 0
    assert "上下文预算不足" in budget.startup_problem()


def _builder_settings(**overrides):
    values = {"context_window_tokens": 6000, "max_output_tokens": 300,
              "max_user_input_tokens": 300, "knowledge_top_k": 1,
              "workflow_agent_max_steps": 1}
    values.update(overrides)
    return _settings(values)


def test_layer1_degrade_moves_only_anchor_and_keeps_recent_raw():
    from app.context import ContextBuilder
    from app.storage import ConversationStore
    from tests.support import sqlite_factory

    with sqlite_factory() as factory:
        store = ConversationStore(factory)
        cid = store.start_turn(user_id="u", message="第一问")
        assistant = store.add_message(cid, "assistant", content="答" * 300)
        builder = ContextBuilder(store, _builder_settings())
        first = builder.build(cid, store.context_messages(cid), current_id=assistant + 1, turn=2)
        assert first.layer1_from == store.anchors(cid)[1]

        for index in range(5):
            store.add_message(cid, "user", content=f"第{index + 2}问")
            store.add_message(cid, "assistant", content="答" * 300)
        rows = store.context_messages(cid)
        plan = builder.build(cid, rows, current_id=rows[-1]["id"] + 1, turn=8)
        assert plan.degraded is not None and plan.degraded[1] > plan.degraded[0]
        assert store.anchors(cid)[1] == plan.layer1_from
        assert plan.layer1[0]["role"] == "user"
        assert plan.layer1[-1]["content"].startswith("答" * 300)
        assert all(row["id"] >= plan.layer1_from for row in plan.layer1)


def test_layer2_truncates_assistant_but_keeps_user():
    from app.context import ContextBuilder
    from app.storage import ConversationStore
    from tests.support import sqlite_factory

    with sqlite_factory() as factory:
        store = ConversationStore(factory)
        cid = store.start_turn(user_id="u", message="用户原话完整保留")
        long_answer = "客服答复" * 20
        store.add_message(cid, "assistant", content=long_answer)
        second_user = store.add_message(cid, "user", content="第二问")
        store.add_message(cid, "assistant", content="短答")
        store.add_message(cid, "user", content="第三问")
        store.set_layer1_from(cid, second_user)
        rows = store.context_messages(cid)
        plan = ContextBuilder(store, _builder_settings()).build(
            cid, rows, current_id=rows[-1]["id"] + 1, turn=3)
        assert plan.summary_upto == 0
        assert [row["role"] for row in plan.layer2] == ["user", "assistant"]
        assert plan.layer2[0]["content"] == "用户原话完整保留"
        assert plan.layer2[1]["content"] == "客服答复" * 10 + "…"


def test_injected_summaries_shrink_the_history_budget():
    from app.context import ContextBuilder
    from app.storage import ConversationStore
    from tests.support import sqlite_factory

    with sqlite_factory() as factory:
        store = ConversationStore(factory)
        cid = store.start_turn(user_id="u", message="订单 1001 要退款")
        for index in range(6):
            store.add_message(cid, "assistant", content="答" * 120)
        builder = ContextBuilder(store, _builder_settings(context_window_tokens=5000))
        plain = builder.build(cid, store.context_messages(cid), current_id=999, turn=7)
        store.append_summary(cid, from_msg_id=0, upto_msg_id=3, content="梗概" * 200)
        with_summary = builder.build(cid, store.context_messages(cid), current_id=1000, turn=8)
        assert with_summary.summary_tokens > plain.summary_tokens == 0
        # 注入的梗概要从历史预算里扣：段数越多，留给原文层与截短层的额度越小。
        assert with_summary.layer2_budget < plain.layer2_budget


def test_model_messages_put_summary_and_evidence_after_current_user():
    from app.context import build_model_messages

    plan = {"summary": "订单 1001 要退款。", "layer1": [], "layer2": [],
            "current_id": 9, "turn": 2}
    trailing = "【知识库证据】\n[1] 七天无理由"
    messages = build_model_messages(plan, "这单能退吗", trailing, _settings({}))
    assert messages[0].type == "system"
    assert messages[1].content == "这单能退吗"
    assert "【会话摘要】\n订单 1001 要退款。" in messages[2].content
    assert messages[2].content.index("【会话摘要】") < messages[2].content.index("【知识库证据】")
