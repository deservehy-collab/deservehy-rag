import re
from datetime import date

import pytest
from sqlalchemy import create_engine

from app.database import build_session_factory
from app.models import Base, Ticket
from app.seed import seed_faq
from app.storage import ConversationStore
from app.tools import TOOL_LABELS, make_tools
from tests.support import FakeKnowledge


@pytest.fixture()
def toolchain(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'tools.db'}")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    seed_faq(factory)
    store = ConversationStore(factory)
    conversation_id = store.start_turn(user_id="anon-1", message="订单 1001 的物流到哪了")
    knowledge = FakeKnowledge()
    tools = make_tools(store, conversation_id, knowledge)
    yield factory, store, conversation_id, knowledge, tools
    engine.dispose()


def test_registry_exposes_the_four_builtin_business_tools(toolchain):
    *_, tools = toolchain
    assert list(tools) == ["query_order", "query_product", "query_faq", "create_ticket"]
    # 物流已移交 MCP Server，但徽章中文名还留在这里给同名工具用。
    assert TOOL_LABELS["query_logistics"] == "查询物流"
    assert set(tools) <= set(TOOL_LABELS)


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


def test_query_faq_keeps_contract_and_reaches_shipping_fee(toolchain):
    tools = toolchain[4]
    hit = tools["query_faq"].invoke({"keyword": "邮费是多少"})
    shipping = hit
    miss = tools["query_faq"].invoke({"keyword": "发票"})
    assert hit["ok"] is True
    assert {"n", "id", "question", "answer", "category", "section_path", "content_type", "scores"} == set(hit["data"]["items"][0])
    assert shipping["ok"] is True
    assert shipping["data"]["canonical_query"] == "普通地区邮费标准"
    assert "邮费" in shipping["data"]["items"][0]["answer"]
    assert miss["ok"] is False and miss["error"] == "low_confidence"
    assert miss["reason"] == "no_hits"


def test_query_faq_reports_unavailable_knowledge_base(toolchain):
    knowledge, tools = toolchain[3], toolchain[4]
    knowledge.error = RuntimeError("Milvus 挂了")
    result = tools["query_faq"].invoke({"keyword": "邮费"})
    assert result["ok"] is False
    assert result["error"] == "knowledge_unavailable"
    assert "知识库" in result["message"]


def test_create_ticket_writes_row_with_server_side_conversation(toolchain):
    factory, _, conversation_id, _, tools = toolchain
    result = tools["create_ticket"].invoke({"description": "杯子送达时已经碎了，希望换货。", "ticket_type": "售后"})
    assert result["ok"] is True
    ticket_no = result["data"]["ticket_no"]
    assert re.fullmatch(rf"T{date.today():%Y%m%d}\d{{3}}", ticket_no)
    with factory() as session:
        ticket = session.get(Ticket, ticket_no)
        assert ticket.conversation_id == conversation_id
        assert ticket.status == "待处理"


def test_category_reaches_the_retriever(toolchain):
    knowledge, tools = toolchain[3], toolchain[4]
    seen = {}
    original = knowledge.retrieve

    def capture(query, category=None):
        seen["category"] = category
        return original(query, category=category)

    knowledge.retrieve = capture
    assert tools["query_faq"].invoke({"keyword": "邮费", "category": "配送与运费"})["ok"] is True
    assert seen["category"] == "配送与运费"
