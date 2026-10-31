from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.storage import ConversationStore
from app.tools.registry import ToolSpec

TOOL_LABELS = {
    "query_order": "查询订单",
    "query_product": "查询商品",
    "query_logistics": "查询物流",
    "query_faq": "查询常见问题",
    "create_ticket": "创建工单",
}
WRITE_TOOLS = {"create_ticket"}

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
MISSING_TICKET_TYPE = "售后"
TICKET_TYPES = ("售后", "投诉", "咨询")


def list_orders() -> list[dict]:
    """演示数据；真实系统应按用户查订单表。"""
    return [dict(order) for order in ORDERS.values()]


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
    # 三个合法取值此前只写在给模型看的那句说明里；规格 §7.2 要 JSON Schema 能拦「取值越界」，
    # 所以把名单挂进 schema。用 json_schema_extra 而不是 Literal：pydantic 侧的运行时校验一字不改
    # （`ticket_type` 为空仍按 MISSING_TICKET_TYPE 兜底），只有校验器与模型能看见这份约束。
    ticket_type: str = Field(description=f"工单类型，只能是：{'、'.join(TICKET_TYPES)}",
                             json_schema_extra={"enum": list(TICKET_TYPES)})


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

    return {item.name: item for item in (query_order, query_product, query_faq, create_ticket)}


def builtin_json_schemas() -> list[dict]:
    return [model.model_json_schema() for model in (OrderArgs, ProductArgs, FaqArgs, TicketArgs)]


def build_specs(store, conversation_id: int, knowledge=None, *, settings=None) -> list[ToolSpec]:
    """内置工具在每次构造上下文时登记进注册表：三样齐全，权限按本地规则给。

    `settings` 是留给 T5 权限白名单的落点，本轮只走默认值。
    """
    tools = make_tools(store, conversation_id, knowledge)
    specs = []
    for name, item in tools.items():
        specs.append(ToolSpec(
            name=name,
            description=item.description or "",
            json_schema=item.args_schema.model_json_schema(),
            source="builtin",
            mcp_server=None,
            permission="write" if name in WRITE_TOOLS else "read",
            label=TOOL_LABELS.get(name, name),
            tool=item,
        ))
    return specs
