from app.tools.builtin import OrderArgs, ProductArgs, TicketArgs
from app.tools.validate import validate_arguments

ORDER = OrderArgs.model_json_schema()
TICKET = TicketArgs.model_json_schema()
PRODUCT = ProductArgs.model_json_schema()


def test_valid_arguments_pass_silently():
    assert validate_arguments(ORDER, {"order_id": "1001"}).ok is True
    assert validate_arguments(ORDER, {"order_id": "1001"}).message == ""


def test_missing_required_field_names_the_field_and_expectation():
    report = validate_arguments(ORDER, {})
    assert report.ok is False
    assert "order_id" in report.message
    assert "缺失" in report.message
    assert "订单号" in report.message            # 取自 schema 里的 description
    assert report.message.endswith("请向用户确认后重新调用。")


def test_wrong_type_is_reported_in_chinese():
    report = validate_arguments(ORDER, {"order_id": 1001})
    assert report.ok is False
    assert "order_id" in report.message and "string" in report.message and "int" in report.message


def test_every_missing_required_field_is_named_once():
    """两个字段同时缺失时不许把同一个字段报两遍——模型照着问就会问错话。"""
    report = validate_arguments(TICKET, {})
    assert "description 缺失" in report.message and "ticket_type 缺失" in report.message
    assert report.message.count("缺失") == 2
    assert "required property" not in report.message      # 英文原文不许漏进给模型的中文说明


def test_out_of_range_and_enum_violations_are_caught():
    too_short = validate_arguments(TICKET, {"description": "碎", "ticket_type": "售后"})
    assert too_short.ok is False and "description" in too_short.message
    assert "长度不在范围内" in too_short.message and "5–500" in too_short.message
    bad_enum = validate_arguments(TICKET, {"description": "杯子碎了想换货", "ticket_type": "退货"})
    assert bad_enum.ok is False and "售后" in bad_enum.message and "投诉" in bad_enum.message
    too_long = validate_arguments(TICKET, {"description": "长" * 600, "ticket_type": "售后"})
    assert too_long.ok is False and "500" in too_long.message


def test_unexpected_extra_field_is_rejected_for_builtin_schemas():
    report = validate_arguments(ORDER, {"order_id": "1001", "extra": 1})
    assert report.ok is False and "多余" in report.message
    assert "extra" in report.message and "Additional properties" not in report.message


def test_mcp_schema_is_used_verbatim_without_tightening_extra_properties():
    """规格 §7.1：只按 Server 给的 schema 校验，不注入 additionalProperties:false。"""
    schema = {"type": "object", "properties": {"order_id": {"type": "string", "description": "订单号"}},
              "required": ["order_id"]}
    assert validate_arguments(schema, {"order_id": "1001", "carrier": "SF"}).ok is True


def test_non_dict_input_is_refused():
    assert validate_arguments(ORDER, "1001").ok is False       # type: ignore[arg-type]


def test_product_needs_at_least_one_of_two_optionals_via_pydantic_not_schema():
    # JSON Schema 表达不了跨字段约束：这里必须放行，由引擎把 pydantic 错误归到参数类。
    assert validate_arguments(PRODUCT, {}).ok is True
