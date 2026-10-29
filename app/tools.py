import asyncio
import json
import time
from dataclasses import dataclass

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy.exc import OperationalError

from app.storage import ConversationStore

TOOL_LABELS = {
    "query_order": "查询订单",
    "query_product": "查询商品",
    "query_logistics": "查询物流",
    "query_faq": "查询常见问题",
    "create_ticket": "创建工单",
}
RESTRICTED_TOOLS = {"query_order", "query_product", "query_logistics", "query_faq"}

ORDERS = {
    "1001": {
        "order_id": "1001",
        "status": "已发货",
        "amount": "199.00",
        "created_at": "2026-10-21 10:12",
        "items": [{"product_id": "P1001", "name": "不锈钢保温杯 500ml", "quantity": 1}],
    },
    "1002": {
        "order_id": "1002",
        "status": "待发货",
        "amount": "89.00",
        "created_at": "2026-10-27 16:40",
        "items": [{"product_id": "P2001", "name": "棉质家居袜 3 双装", "quantity": 1}],
    },
}
PRODUCTS = {
    "P1001": {
        "product_id": "P1001",
        "name": "不锈钢保温杯 500ml",
        "price": "199.00",
        "stock": 128,
        "highlights": ["316 不锈钢内胆", "保温 12 小时", "420g 轻量杯身"],
    },
    "P2001": {
        "product_id": "P2001",
        "name": "棉质家居袜 3 双装",
        "price": "89.00",
        "stock": 0,
        "highlights": ["精梳棉", "无骨缝头", "四季可穿"],
    },
}
LOGISTICS = {
    "1001": {
        "order_id": "1001",
        "carrier": "顺丰速运",
        "tracking_no": "SF1234567890",
        "status": "运输中",
        "nodes": [
            {"time": "2026-10-26 09:20", "text": "快件已到达【杭州转运中心】"},
            {"time": "2026-10-27 08:05", "text": "快件已到达【上海分拨中心】，准备派送"},
        ],
    },
    "1002": {
        "order_id": "1002",
        "carrier": "中通快递",
        "tracking_no": "ZT9876543210",
        "status": "已揽收",
        "nodes": [{"time": "2026-10-27 18:30", "text": "快件已揽收，等待发出"}],
    },
}

MISSING_TICKET_TYPE = "售后"
MAX_SUMMARY = 60


class OrderArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: str = Field(min_length=1, max_length=64, description="订单号，例如 1001")


class ProductArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: str | None = Field(default=None, max_length=64, description="商品 ID，例如 P1001")
    keyword: str | None = Field(default=None, max_length=64, description="商品名称关键词，例如 保温杯")

    @model_validator(mode="after")
    def require_one(self):
        if not (self.product_id or self.keyword):
            raise ValueError("product_id 和 keyword 至少填写一个。")
        return self


class FaqArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keyword: str = Field(min_length=1, max_length=200, description="要查询的政策问题或关键词，尽量保留用户原话")
    category: str | None = Field(default=None, max_length=255, description="可选分类过滤，例如 配送与运费")


class TicketArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=5, max_length=500, description="用户的问题和诉求，尽量保留用户原话")
    ticket_type: str = Field(description="工单类型，只能是：售后、投诉、咨询")


@dataclass(frozen=True)
class ToolOutcome:
    tool: str
    ok: bool
    code: str
    content: str
    summary: str
    duration_ms: int


class ToolExecutor:
    def __init__(
        self,
        tools: dict[str, BaseTool],
        *,
        timeout: float,
        attempts: int,
        timeouts: dict[str, float] | None = None,
    ):
        self.tools = tools
        self.timeout = timeout
        self.attempts = attempts
        self.timeouts = dict(timeouts or {})

    async def execute(self, name: str, args: dict) -> ToolOutcome:
        started = time.perf_counter()
        tool = self.tools.get(name)
        if tool is None:
            return self._failure(name, "unknown_tool", f"未知工具 {name}", started)
        try:
            validated = tool.args_schema.model_validate(args)
        except ValidationError:
            return self._failure(name, "invalid_arguments", "工具参数不完整或不合法。", started)

        result: dict | None = None
        code = "tool_error"
        message = "工具执行失败，请稍后重试。"
        limit = self.timeouts.get(name, self.timeout)
        # 写工具超时后线程可能仍在执行，重试会造成重复工单，因此只重试只读工具。
        attempts = self.attempts if name in RESTRICTED_TOOLS else 1
        for _ in range(attempts):
            try:
                result = await asyncio.wait_for(asyncio.to_thread(tool.invoke, validated.model_dump()), limit)
            except TimeoutError:
                code = "tool_timeout"
                message = "工具响应超时，请稍后重试。"
            except OperationalError:
                code = "tool_error"
                message = "数据库暂时不可用，请稍后重试。"
            except Exception:
                return self._failure(name, "tool_error", "工具执行失败，请稍后重试。", started)
            else:
                return self._outcome(name, result, started)

        return self._failure(name, code, message, started)

    def _outcome(self, name: str, result: object, started: float) -> ToolOutcome:
        payload = json.dumps(result, ensure_ascii=False, default=str)
        # 工具用 {"ok": false, ...} 表达“查不到”，对模型和页面都按失败处理，但仍把结果原样回灌。
        if isinstance(result, dict) and result.get("ok") is False:
            message = str(result.get("message") or "工具没有返回结果。")
            return ToolOutcome(
                tool=name,
                ok=False,
                code=str(result.get("error") or "tool_error"),
                content=payload,
                summary=message[:MAX_SUMMARY],
                duration_ms=_elapsed(started),
            )
        return ToolOutcome(
            tool=name,
            ok=True,
            code="ok",
            content=payload,
            summary=summarize(name, result if isinstance(result, dict) else {}),
            duration_ms=_elapsed(started),
        )

    def _failure(self, name: str, code: str, message: str, started: float) -> ToolOutcome:
        payload = {"ok": False, "error": code, "message": message}
        return ToolOutcome(
            tool=name,
            ok=False,
            code=code,
            content=json.dumps(payload, ensure_ascii=False),
            summary=message[:MAX_SUMMARY],
            duration_ms=_elapsed(started),
        )


def _elapsed(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def summarize(name: str, result: dict) -> str:
    if not result.get("ok"):
        return str(result.get("message") or "工具没有返回结果。")[:MAX_SUMMARY]
    data = result.get("data") or {}
    if name == "query_order":
        return f"订单 {data.get('order_id')} {data.get('status')}"
    if name == "query_logistics":
        nodes = data.get("nodes") or []
        latest = nodes[-1]["text"] if nodes else "暂无节点"
        return f"{len(nodes)} 条物流记录，最新：{latest}"[:MAX_SUMMARY]
    if name == "query_product":
        items = data.get("items") or []
        return f"{len(items)} 个商品匹配：{items[0]['name']}" if items else "未找到商品"
    if name == "query_faq":
        return f"命中 {len(data.get('items') or [])} 条店铺政策"
    if name == "create_ticket":
        return f"工单 {data.get('ticket_no')} 已创建"
    return "工具已执行"


def make_tools(
    store: ConversationStore, conversation_id: int, knowledge=None
) -> dict[str, BaseTool]:
    @tool("query_order", args_schema=OrderArgs, description="按订单号查询订单状态、下单时间、金额和商品明细。用户给出订单号时使用。")
    def query_order(order_id: str) -> dict:
        order = ORDERS.get(order_id.strip())
        if order is None:
            return {"ok": False, "error": "not_found", "message": f"没有找到订单 {order_id}。"}
        return {"ok": True, "data": order}

    @tool("query_product", args_schema=ProductArgs, description="按商品 ID 或名称关键词查询商品价格、库存和卖点。")
    def query_product(product_id: str | None = None, keyword: str | None = None) -> dict:
        wanted_id = (product_id or "").strip().upper()
        needle = (keyword or "").strip()
        items = [
            product
            for product in PRODUCTS.values()
            if (wanted_id and product["product_id"] == wanted_id) or (needle and needle in product["name"])
        ]
        if not items:
            query = wanted_id or needle
            return {"ok": False, "error": "not_found", "message": f"没有找到商品 {query}。"}
        return {"ok": True, "data": {"items": items}}

    @tool("query_logistics", args_schema=OrderArgs, description="按订单号查询承运商、运单号和物流节点。用户问包裹到哪了时使用。")
    def query_logistics(order_id: str) -> dict:
        record = LOGISTICS.get(order_id.strip())
        if record is None:
            return {"ok": False, "error": "not_found", "message": f"没有找到订单 {order_id} 的物流记录。"}
        return {"ok": True, "data": record}

    @tool("query_faq", args_schema=FaqArgs, description="按用户问题检索店铺知识库：退换货、退款、发票、保修、运费、时效、型号与配件兼容性等政策。")
    def query_faq(keyword: str, category: str | None = None) -> dict:
        if knowledge is None:
            return {"ok": False, "error": "knowledge_unavailable", "message": "知识库暂时不可用，请稍后重试。"}
        try:
            result = knowledge.retrieve(keyword, category=category)
        except Exception:
            return {"ok": False, "error": "knowledge_unavailable", "message": "知识库暂时不可用，请稍后重试。"}
        if result.low_confidence:
            return {"ok": False, "error": "low_confidence",
                    "message": "知识库没有足够依据回答这个问题。", "reason": result.reason}
        return {"ok": True, "data": {
            "items": [item.item() for item in result.evidence],
            "canonical_query": result.canonical_query,
            "low_confidence": False,
        }}

    @tool("create_ticket", args_schema=TicketArgs, description="用户的问题需要人工跟进时创建工单。服务端会自动关联当前会话，只需填写问题描述和类型。")
    def create_ticket(description: str, ticket_type: str) -> dict:
        try:
            ticket_no = store.create_ticket(conversation_id, description.strip(), ticket_type.strip() or MISSING_TICKET_TYPE)
        except RuntimeError as exc:
            return {"ok": False, "error": "tool_error", "message": str(exc)}
        return {"ok": True, "data": {"ticket_no": ticket_no, "status": "待处理"}}

    return {item.name: item for item in (query_order, query_product, query_logistics, query_faq, create_ticket)}
