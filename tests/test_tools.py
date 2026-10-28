import asyncio
import json
import re
import time
from datetime import date

import pytest
from langchain_core.tools import tool
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError

from app.database import build_session_factory
from app.models import Base, Ticket
from app.seed import seed_faq
from app.storage import ConversationStore
from app.tools import OrderArgs, TOOL_LABELS, ToolExecutor, make_tools


@pytest.fixture()
def toolchain(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'tools.db'}")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    seed_faq(factory)
    store = ConversationStore(factory)
    conversation_id = store.start_turn(user_id="anon-1", message="订单 1001 的物流到哪了")
    tools = make_tools(store, conversation_id)
    yield factory, store, conversation_id, tools
    engine.dispose()


def test_registry_exposes_the_five_business_tools(toolchain):
    *_, tools = toolchain
    assert list(tools) == ["query_order", "query_product", "query_logistics", "query_faq", "create_ticket"]
    assert TOOL_LABELS["query_logistics"] == "查询物流"
    assert set(TOOL_LABELS) == set(tools)


def test_query_order_returns_known_order(toolchain):
    *_, tools = toolchain
    result = tools["query_order"].invoke({"order_id": "1001"})
    assert result["ok"] is True
    assert result["data"]["order_id"] == "1001"
    assert result["data"]["status"] == "已发货"
    assert result["data"]["items"]


def test_query_order_reports_missing_order(toolchain):
    *_, tools = toolchain
    result = tools["query_order"].invoke({"order_id": "9999"})
    assert result["ok"] is False
    assert result["error"] == "not_found"
    assert "9999" in result["message"]


def test_query_logistics_returns_nodes(toolchain):
    *_, tools = toolchain
    result = tools["query_logistics"].invoke({"order_id": "1001"})
    assert result["ok"] is True
    assert result["data"]["carrier"] == "顺丰速运"
    assert len(result["data"]["nodes"]) == 2
    assert "上海" in result["data"]["nodes"][-1]["text"]


def test_query_product_matches_by_id_and_keyword(toolchain):
    *_, tools = toolchain
    by_id = tools["query_product"].invoke({"product_id": "P1001"})
    by_keyword = tools["query_product"].invoke({"keyword": "保温杯"})
    assert by_id["ok"] is True
    assert by_id["data"]["items"][0]["product_id"] == "P1001"
    assert by_keyword["ok"] is True
    assert by_keyword["data"]["items"][0]["product_id"] == "P1001"
    missing = tools["query_product"].invoke({"keyword": "不存在的商品"})
    assert missing["ok"] is False and missing["error"] == "not_found"


def test_query_faq_hits_return_policy_and_misses_shipping_fee(toolchain):
    *_, tools = toolchain
    hit = tools["query_faq"].invoke({"keyword": "退货"})
    miss = tools["query_faq"].invoke({"keyword": "邮费"})
    assert hit["ok"] is True
    assert any("退货" in item["question"] for item in hit["data"]["items"])
    assert miss["ok"] is False and miss["error"] == "no_match"


def test_create_ticket_writes_row_with_server_side_conversation(toolchain):
    factory, _, conversation_id, tools = toolchain
    result = tools["create_ticket"].invoke({"description": "杯子送达时已经碎了，希望换货。", "ticket_type": "售后"})
    assert result["ok"] is True
    ticket_no = result["data"]["ticket_no"]
    assert re.fullmatch(rf"T{date.today():%Y%m%d}\d{{3}}", ticket_no)
    with factory() as session:
        ticket = session.get(Ticket, ticket_no)
        assert ticket.conversation_id == conversation_id
        assert ticket.status == "待处理"


def test_executor_rejects_unknown_tool_and_bad_arguments(toolchain):
    *_, tools = toolchain
    executor = ToolExecutor(tools, timeout=1, attempts=2)
    unknown = asyncio.run(executor.execute("query_weather", {}))
    invalid = asyncio.run(executor.execute("query_order", {"order_id": ""}))
    missing = asyncio.run(executor.execute("query_product", {}))
    assert unknown.ok is False and unknown.code == "unknown_tool"
    assert invalid.ok is False and invalid.code == "invalid_arguments"
    assert missing.ok is False and missing.code == "invalid_arguments"


def test_executor_summarizes_success_for_the_badge(toolchain):
    *_, tools = toolchain
    executor = ToolExecutor(tools, timeout=1, attempts=2)
    outcome = asyncio.run(executor.execute("query_logistics", {"order_id": "1001"}))
    assert outcome.ok is True
    assert "2 条物流记录" in outcome.summary
    assert "上海" in outcome.summary
    assert outcome.duration_ms >= 0
    assert json.loads(outcome.content)["data"]["carrier"] == "顺丰速运"


def test_executor_retries_transient_failures(toolchain):
    *_, tools = toolchain
    calls = []

    @tool("query_order", args_schema=OrderArgs)
    def flaky(order_id: str) -> dict:
        """测试用：第一次失败，第二次成功。"""
        calls.append(order_id)
        if len(calls) == 1:
            raise OperationalError("select 1", {}, Exception("数据库忙"))
        return {"ok": True, "data": {"order_id": order_id}}

    executor = ToolExecutor({**tools, "query_order": flaky}, timeout=1, attempts=2)
    outcome = asyncio.run(executor.execute("query_order", {"order_id": "1001"}))
    assert outcome.ok is True
    assert calls == ["1001", "1001"]


def test_executor_times_out_and_retries_reads_only(toolchain):
    *_, tools = toolchain
    reads = []
    writes = []

    @tool("query_order", args_schema=OrderArgs)
    def slow_read(order_id: str) -> dict:
        """测试用：慢查询。"""
        reads.append(order_id)
        time.sleep(0.3)
        return {"ok": True, "data": {"order_id": order_id}}

    @tool("create_ticket")
    def slow_write(description: str, ticket_type: str) -> dict:
        """测试用：慢写入。"""
        writes.append(description)
        time.sleep(0.3)
        return {"ok": True, "data": {"ticket_no": "T1"}}

    executor = ToolExecutor({**tools, "query_order": slow_read, "create_ticket": slow_write}, timeout=0.05, attempts=3)
    read_outcome = asyncio.run(executor.execute("query_order", {"order_id": "1001"}))
    write_outcome = asyncio.run(executor.execute("create_ticket", {"description": "杯子碎了", "ticket_type": "售后"}))
    assert read_outcome.ok is False and read_outcome.code == "tool_timeout"
    assert len(reads) == 3
    assert write_outcome.ok is False and write_outcome.code == "tool_timeout"
    assert len(writes) == 1
