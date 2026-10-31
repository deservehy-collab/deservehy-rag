import re
from dataclasses import dataclass

from jsonschema import Draft202012Validator

_SUFFIX = "请向用户确认后重新调用。"
_REQUIRED_PATTERN = re.compile(r"'(?P<name>[^']+)' is a required property")
_NUMERIC_WORDS = {
    "minimum": "不小于",
    "exclusiveMinimum": "大于",
    "maximum": "不大于",
    "exclusiveMaximum": "小于",
}


@dataclass(frozen=True)
class ValidationReport:
    ok: bool
    message: str


def _spec_at(schema: dict, path: list) -> dict:
    node = schema
    for step in path:
        node = (node.get("properties") or {}).get(step) or {} if isinstance(node, dict) else {}
    return node


def _span(spec: dict) -> str:
    low, high = spec.get("minLength"), spec.get("maxLength")
    if low is not None and high is not None:
        return f"{low}–{high}"
    if low is not None:
        return f"至少 {low}"
    if high is not None:
        return f"最多 {high}"
    return "限定"


def _describe(error, schema: dict) -> str:
    path = list(error.absolute_path)
    field = ".".join(str(item) for item in path) or "（根）"
    spec = _spec_at(schema, path)
    hint = spec.get("description") or ""
    tail = f"，{hint}" if hint else ""
    kind = error.validator
    if kind == "required":
        # jsonschema 不给「缺的是哪一个」留结构位：schema_path 恒为 ['required']，validator_value 是整份
        # 清单，属性名只出现在 message 开头那个引号里（4.26.0 实测）。取不到就退回清单里第一个还没出现的键，
        # 绝不把英文原文塞进给模型的中文说明。
        matched = _REQUIRED_PATTERN.match(error.message)
        if matched:
            missing = matched.group("name")
        else:
            present = error.instance if isinstance(error.instance, dict) else {}
            names = error.validator_value if isinstance(error.validator_value, list) else []
            missing = next((name for name in names if name not in present), field)
        wanted = ((schema.get("properties") or {}).get(missing) or {}).get("description") or ""
        return f"{missing} 缺失（期望：{wanted or '必填项'}）"
    if kind == "type":
        expected = error.validator_value
        expected = "、".join(expected) if isinstance(expected, list) else str(expected)
        return f"{field} 类型不对（期望 {expected}，实际 {type(error.instance).__name__}）"
    if kind == "enum":
        return f"{field} 取值越界，只能是：{'、'.join(str(item) for item in error.validator_value)}"
    if kind == "additionalProperties":
        allowed = set(spec.get("properties") or {})
        offending = sorted(str(key) for key in (error.instance or {}) if key not in allowed)
        return f"{field} 带了多余的参数：{'、'.join(offending) or '未命名参数'}"
    if kind in {"minLength", "maxLength"}:
        return f"{field} 长度不在范围内（{_span(spec)} 字{tail}）"
    if kind in _NUMERIC_WORDS:
        return f"{field} 数值越界（应{_NUMERIC_WORDS[kind]} {error.validator_value}{tail}）"
    if kind == "pattern":
        return f"{field} 格式不符合要求（应匹配 {error.validator_value}）"
    return f"{field} 不符合 {kind} 校验规则"


def validate_arguments(schema: dict, args: dict) -> ValidationReport:
    if not isinstance(args, dict):
        return ValidationReport(False, "参数校验未通过：调用参数必须是对象（键值对）。" + _SUFFIX)
    errors = sorted(Draft202012Validator(schema).iter_errors(args),
                    key=lambda item: list(item.absolute_path))
    if not errors:
        return ValidationReport(True, "")
    lines = [_describe(error, schema) for error in errors]
    return ValidationReport(False, "参数校验未通过：" + "；".join(lines) + "。" + _SUFFIX)
