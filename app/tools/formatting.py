import json

from langchain_core.messages import ToolMessage

MAX_SUMMARY = 60

# 内部枚举码 → 人话（现状散在 app/tools.py:12-18 与 app/workflow/actions.py:12-16，集中到这一处）
ENUM_LABELS = {
    "order_status": {"SHIPPED": "已发货", "PENDING": "待发货", "PAID": "已付款",
                     "CANCELLED": "已取消", "SIGNED": "已签收"},
    "logistics_status": {"IN_TRANSIT": "运输中", "PICKED_UP": "已揽收", "DELIVERED": "已签收",
                         "ABNORMAL": "异常"},
    "warranty_status": {"IN_WARRANTY": "在保", "OUT_OF_WARRANTY": "已过保"},
    "refund_status": {"PROCESSING": "处理中", "APPROVED": "已通过", "REJECTED": "已驳回",
                      "REFUNDED": "已退款"},
}

# 每个工具留哪些字段：data 顶层 + items 元素；不在册的工具原样透传，绝不因格式化丢关键字段。
PROJECTIONS = {
    "query_order": {"data": ["order_id", "status", "amount", "created_at", "items"],
                    "items": ["product_id", "name", "quantity"]},
    "query_logistics": {"data": ["order_id", "carrier", "tracking_no", "status"], "nodes": 1},
    "query_product": {"data": ["items"], "items": ["product_id", "name", "price", "stock", "highlights"]},
    "query_faq": {"data": ["items", "canonical_query", "low_confidence"],
                  "items": ["n", "id", "question", "answer", "category", "section_path", "content_type"]},
    "create_ticket": {"data": ["ticket_no", "status"]},
    "query_warranty": {"data": ["order_id", "status", "days_left", "expires_on"]},
    "query_refund_progress": {"data": ["order_id", "status", "updated_at", "remark"]},
}

_STATUS_ENUM_KEY = {"query_order": "order_status", "query_logistics": "logistics_status",
                    "query_warranty": "warranty_status", "query_refund_progress": "refund_status"}

_GENERIC_COUNT_KEYS = ("items", "nodes", "list", "records")

# MCP 未登记中文名的工具用 Server 级中文名兜底，避免摘要里出现英文工具名。
_SERVER_LABELS = {"logistics": "查询物流", "aftersale": "查询售后"}


def _translate(tool: str, value):
    key = _STATUS_ENUM_KEY.get(tool)
    if not key or not isinstance(value, str):
        return value
    return ENUM_LABELS[key].get(value.upper(), value)


def _summary_label(spec) -> str:
    if spec.label and spec.label != spec.name:
        return spec.label
    if spec.permission == "read":
        return _SERVER_LABELS.get(spec.mcp_server or "", spec.label or spec.name)
    return spec.label or spec.name


def _text_of(content) -> str:
    """ToolMessage.content 实测有三种：str（内置）、内容块 list（MCP 文本块）、其他。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    return str(content)


def _from_text(text: str, *, errored: bool) -> object:
    if errored:
        return {"ok": False, "error": "tool_error", "message": text[:500] or "工具执行失败。"}
    try:
        parsed = json.loads(text)
    except ValueError:
        # 非 JSON 的裸文本按坏消息如实回灌，模型能直接引用给用户
        return {"ok": False, "error": "tool_error", "message": text[:500] or "工具没有返回内容。"}
    return parsed if isinstance(parsed, dict) else {"ok": True, "data": parsed}


def normalize_result(raw):
    """引擎统一用 tool_call 形状调用，回来的是 ToolMessage。

    实测（.venv，langchain-core 1.7.0 + adapters 0.3.2 + mcp 1.30.0 真进程）：
      · `ainvoke(args_dict)` 对内置返回 dict、对 MCP 只返回 content 字符串，artifact 被丢掉；
      · `ainvoke(tool_call)` 两种来源都返回 ToolMessage；
      · 内置的 content 是紧凑 JSON 字符串，MCP 的 content 是**内容块列表**
        `[{"type": "text", "text": "{...}"}]`（FastMCP 用 indent=2 序列化，中文不转义）；
      · `artifact` 只有 Server 给出 `structuredContent` 时才有，且形如
        `{"structured_content": {...}}`（TypedDict 包装）——我们自己的 Server 返回 `dict`
        实测拿不到，所以主路是解析 content，artifact 只当外部 Server 愿意给时的优先项。
    """
    if isinstance(raw, ToolMessage):
        if raw.status == "error":
            return _from_text(_text_of(raw.content), errored=True)
        artifact = raw.artifact if isinstance(raw.artifact, dict) else None
        structured = (artifact or {}).get("structured_content")
        if isinstance(structured, dict):
            return structured if "ok" in structured else {"ok": True, "data": structured}
        if isinstance(structured, list):
            return {"ok": True, "data": structured}
        return _from_text(_text_of(raw.content), errored=False)
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (str, list)):
        return _from_text(_text_of(raw), errored=False)
    return {"ok": True, "data": raw}


def _project(tool: str, result: dict) -> dict:
    rule = PROJECTIONS.get(tool)
    if not rule or not isinstance(result.get("data"), dict):
        return result
    data = result["data"]
    keep = {key: data[key] for key in rule.get("data", []) if key in data}
    if "status" in keep:
        keep["status"] = _translate(tool, keep["status"])
    if "items" in keep and isinstance(keep["items"], list):
        fields = rule.get("items", [])
        keep["items"] = [{key: item[key] for key in fields if key in item} for item in keep["items"]
                         if isinstance(item, dict)]
    if "nodes" in data and isinstance(data["nodes"], list) and data["nodes"]:
        rule_nodes = rule.get("nodes")
        keep["nodes"] = data["nodes"][-rule_nodes:] if rule_nodes else data["nodes"]
        latest = data["nodes"][-1]
        if isinstance(latest, dict) and "text" in latest:
            keep["latest_node"] = latest["text"]
    return {**result, "data": keep}


def format_content(result, spec) -> str:
    payload = _project(spec.name, result) if isinstance(result, dict) else result
    return json.dumps(payload, ensure_ascii=False, default=str)


def _count(data: dict) -> int | None:
    for key in _GENERIC_COUNT_KEYS:
        if isinstance(data.get(key), list):
            return len(data[key])
    return None


def _clip(text: str) -> str:
    return str(text)[:MAX_SUMMARY]


def summarize(spec, result) -> str:
    if not isinstance(result, dict) or not result.get("ok"):
        message = (result or {}).get("message") if isinstance(result, dict) else None
        return _clip(message or "工具没有返回结果。")
    data = result.get("data")
    if isinstance(data, list):
        data = {"items": data}
    elif not isinstance(data, dict):
        data = {}
    name = spec.name
    if name == "query_order":
        return _clip(f"订单 {data.get('order_id')} {data.get('status')}")
    if name == "query_logistics":
        nodes = data.get("nodes") or []
        latest = nodes[-1].get("text") if nodes and isinstance(nodes[-1], dict) else "暂无节点"
        return _clip(f"{len(nodes)} 条物流记录，最新：{latest}")
    if name == "query_product":
        items = data.get("items") or []
        first = items[0].get("name") if items and isinstance(items[0], dict) else None
        return f"{len(items)} 个商品匹配：{first}" if first else "未找到商品"
    if name == "query_faq":
        return _clip(f"命中 {len(data.get('items') or [])} 条店铺政策")
    if name == "create_ticket":
        return _clip(f"工单 {data.get('ticket_no')} 已创建")
    count = _count(data)
    if count is not None:
        return _clip(f"{_summary_label(spec)}：{count} 条结果")
    key = next((f"{k}={v}" for k, v in data.items() if isinstance(v, (str, int))), "")
    return _clip(f"{_summary_label(spec)}：{key}") if key else _clip(f"{_summary_label(spec)}：已执行")
