import json

from langchain_core.messages import ToolMessage

from app.tools.builtin import TOOL_LABELS
from app.tools.formatting import ENUM_LABELS, format_content, normalize_result, summarize
from app.tools.registry import ToolSpec
from tests.test_tool_policy import make_tool


def spec(name="query_logistics", source="builtin", server=None, permission="read"):
    return ToolSpec(name=name, description="d", json_schema={"type": "object", "properties": {}},
                    source=source, mcp_server=server, permission=permission,
                    label=TOOL_LABELS.get(name, name), tool=make_tool(name))


def test_projection_keeps_only_useful_fields_and_human_labels():
    payload = {"ok": True, "data": {"order_id": "1001", "carrier": "SF", "status": "IN_TRANSIT",
                                    "internal_code": 99,
                                    "nodes": [{"time": "a", "text": "A"}, {"time": "b", "text": "B"}]}}
    projected = json.loads(format_content(payload, spec()))
    assert projected["data"]["status"] == "运输中"
    assert "internal_code" not in projected["data"]
    assert projected["data"]["latest_node"] == "B"
    assert projected["data"].pop("nodes") == [{"time": "b", "text": "B"}]


def test_chinese_is_not_escaped():
    assert "\\u" not in format_content({"ok": True, "data": {"items": [{"name": "保温杯"}]}},
                                       spec("query_product"))


def test_missing_projection_passes_the_payload_through_untouched():
    payload = {"ok": True, "data": {"whatever": [1, 2, 3]}}
    assert json.loads(format_content(payload, spec("brand_new_tool"))) == payload


def test_order_status_enum_is_translated():
    out = json.loads(format_content({"ok": True, "data": {"order_id": "1001", "status": "SHIPPED",
                                                          "amount": "199.00", "created_at": "x",
                                                          "items": [{"product_id": "P1001",
                                                                     "name": "保温杯", "quantity": 1,
                                                                     "note": "内部"}]}},
                                     spec("query_order")))
    assert out["data"]["status"] == ENUM_LABELS["order_status"]["SHIPPED"]
    assert "note" not in out["data"]["items"][0]
    assert out["data"]["items"][0]["product_id"] == "P1001"      # 模型据此接着查商品


def test_tool_message_prefers_the_structured_artifact():
    """实测：adapters 的 artifact 是 {"structured_content": {...}} 包装，不是裸 payload。"""
    msg = ToolMessage(content='{"ok": true, "data": {"n": 1}}', tool_call_id="c1", name="m",
                      artifact={"structured_content": {"ok": True, "data": {"n": 2}}})
    assert normalize_result(msg) == {"ok": True, "data": {"n": 2}}


def test_artifact_is_enveloped_and_never_overrides_an_error():
    bare = ToolMessage(content='{"ok": true, "data": {"n": 1}}', tool_call_id="c1", name="m",
                      artifact={"structured_content": {"n": 2}})
    assert normalize_result(bare) == {"ok": True, "data": {"n": 2}}
    errored = ToolMessage(content="连接被拒绝", tool_call_id="c1", name="m", status="error",
                          artifact={"structured_content": {"ok": True, "data": {"n": 3}}})
    assert normalize_result(errored) == {"ok": False, "error": "tool_error", "message": "连接被拒绝"}


def test_tool_message_without_artifact_falls_back_to_the_content():
    assert normalize_result(ToolMessage(content='{"ok": true, "data": {"n": 1}}',
                                        tool_call_id="c1", name="m")) == {"ok": True, "data": {"n": 1}}
    assert normalize_result(ToolMessage(content="null", tool_call_id="c1", name="m")) == {"ok": True, "data": None}


def test_plain_text_content_survives_as_a_message():
    assert normalize_result(ToolMessage(content="查不到这单", tool_call_id="c1", name="m")) == {
        "ok": False, "error": "tool_error", "message": "查不到这单"}
    assert normalize_result(ToolMessage(content="连接被拒绝", tool_call_id="c1", name="m",
                                        status="error"))["message"] == "连接被拒绝"


def test_mcp_content_blocks_are_read_as_text():
    """真进程实测：FastMCP 的 dict 返回值到客户端是内容块列表，不是字符串。"""
    msg = ToolMessage(content=[{"type": "text", "text": '{\n  "ok": true,\n  "data": {"n": 4}\n}',
                                "id": "lc_1"}], tool_call_id="c1", name="query_logistics")
    assert normalize_result(msg) == {"ok": True, "data": {"n": 4}}


def test_builtin_dict_and_bare_json_string_both_normalize():
    assert normalize_result({"ok": True, "data": {"n": 1}}) == {"ok": True, "data": {"n": 1}}
    assert normalize_result('{"ok": true, "data": {"n": 3}}') == {"ok": True, "data": {"n": 3}}


def test_summaries_use_builtin_templates_and_generic_fallback():
    assert "2 条物流记录" in summarize(spec(), {"ok": True, "data": {"nodes": [{"text": "A"}, {"text": "上海"}]}})
    assert summarize(spec("query_faq"), {"ok": True, "data": {"items": [1, 2]}}) == "命中 2 条店铺政策"
    assert summarize(spec("create_ticket"), {"ok": True, "data": {"ticket_no": "T1"}}) == "工单 T1 已创建"
    generic = summarize(spec("query_station_info", source="mcp", server="logistics"),
                        {"ok": True, "data": {"items": [1, 2, 3]}})
    assert generic.startswith("查询物流：") and "3" in generic
    assert summarize(spec(), {"ok": False, "message": "没有找到订单 9999 的物流记录。"}) == "没有找到订单 9999 的物流记录。"
    assert len(summarize(spec(), {"ok": True, "data": {"nodes": [{"text": "长" * 200}]}})) <= 60


def test_every_summary_path_clips_to_60_and_list_data_does_not_crash():
    long_order = {"ok": True, "data": {"order_id": "9" * 64, "status": "SHIPPED"}}
    assert len(summarize(spec("query_order"), long_order)) <= 60
    generic = spec("query_station_info", source="mcp", server="logistics")
    assert summarize(generic, {"ok": True, "data": [1, 2, 3]}).startswith("查询物流：3")
    write = spec("create_refund", source="mcp", server="aftersale", permission="write")
    assert summarize(write, {"ok": True, "data": {"items": [1]}}).startswith("create_refund：")
    assert summarize(spec("brand_new_tool"), {"ok": True, "data": {}}) == "brand_new_tool：已执行"
