import pytest
from sqlalchemy import create_engine

from app.database import build_session_factory
from app.models import Base
from app.seed import seed_faq
from app.storage import ConversationStore
from app.tools.builtin import builtin_json_schemas, build_specs
from app.tools.registry import DuplicateToolError, ToolRegistry, ToolSpec, merge_specs
from tests.support import FakeKnowledge


@pytest.fixture()
def specs(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'reg.db'}")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    seed_faq(factory)
    store = ConversationStore(factory)
    conversation_id = store.start_turn(user_id="anon-1", message="帮我看看")
    result = build_specs(store, conversation_id, FakeKnowledge())
    engine.dispose()
    return result


def test_every_spec_carries_the_three_required_pieces(specs):
    assert {item.name for item in specs} == {"query_order", "query_product",
                                             "query_faq", "create_ticket"}
    for item in specs:
        assert item.description.strip(), item.name
        assert item.json_schema["type"] == "object"
        assert item.json_schema["properties"], item.name
        assert item.source == "builtin" and item.mcp_server is None
        assert item.permission == ("write" if item.name == "create_ticket" else "read")
        assert item.label, item.name


def test_builtin_schema_estimate_covers_every_local_tool():
    schemas = builtin_json_schemas()
    assert len(schemas) == 4          # 四个参数模型：订单、商品、知识库、工单
    assert all(item["type"] == "object" for item in schemas)


def test_each_tool_carries_its_own_parameter_schema(specs):
    """三样里最容易被糊弄的一行：每个工具的 schema 必须各归各的，全用同一个模型也照样能过上一条测试。"""
    by_name = {item.name: item for item in specs}
    expected = {
        "query_order": {"order_id"},
        "query_product": {"product_id", "keyword"},
        "query_faq": {"keyword", "category"},
        "create_ticket": {"description", "ticket_type"},
    }
    for name, fields in expected.items():
        assert set(by_name[name].json_schema["properties"]) == fields, name
    assert by_name["create_ticket"].description != by_name["query_order"].description


def test_registry_rejects_duplicate_names(specs):
    with pytest.raises(DuplicateToolError):
        ToolRegistry([*specs, specs[0]])


def test_missing_required_field_is_refused_at_construction(specs):
    base = dict(specs[0].__dict__)
    for field, empty in (("description", ""), ("json_schema", {})):
        broken = ToolSpec(**{**base, "name": f"坏工具-{field}", field: empty})
        with pytest.raises(ValueError, match=field):
            ToolRegistry([broken])


def test_registry_hands_back_the_same_table_it_was_given(specs):
    registry = ToolRegistry(specs)
    assert registry.names() == [item.name for item in specs]
    assert [item.name for item in registry.specs()] == registry.names()
    assert [item.name for item in registry.langchain_tools()] == registry.names()
    assert all(hasattr(item, "ainvoke") for item in registry.langchain_tools())
    assert registry.get("query_faq").label == "查询常见问题"
    assert registry.get("没有这个工具") is None


def test_merge_keeps_the_first_and_reports_what_it_dropped(specs):
    winner = specs[0]
    # clone 只改 name 的话它和 winner 完全等价，「保留最后一个但保持顺序」的实现照样能绿——
    # 所以必须在看得见的位置（label）上不同，并断言留下来的是 winner 这个对象本身。
    clone = ToolSpec(**{**winner.__dict__, "label": "冒牌同名工具"})
    kept, dropped = merge_specs([specs, [clone]])
    assert kept[0] is winner
    assert kept == list(specs)
    assert dropped == [winner.name]
