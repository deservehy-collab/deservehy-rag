from app.agent.guards import ACTION_LABELS

REFUND_REASONS = (
    "商品破损或质量问题",
    "尺寸或颜色不合适",
    "商品与描述不符",
    "少件或错发",
    "不想要了",
    "其他",
)

TICKET_TYPES = {"退款退货": "售后", "售后": "售后", "投诉": "投诉"}


def offer_ticket_type(intent: str) -> str:
    return TICKET_TYPES.get(intent, "咨询")


def build_offer(turn: int, actions: list[str], question: str, intent: str, order: dict | None = None) -> dict:
    ticket = None
    if "ticket" in actions:
        kind = offer_ticket_type(intent)
        prefix = "用户投诉" if kind == "投诉" else "用户反馈"
        ticket = {"type": kind, "description": f"{prefix}：{question}"[:500]}
    refund = None
    if "refund" in actions and order:
        refund = {"order_id": str(order.get("order_id") or ""), "reasons": list(REFUND_REASONS)}
    return {"turn": turn, "actions": list(actions), "ticket": ticket, "ticket_no": None,
            "refund": refund, "refund_no": None}


def actions_payload(actions: list[str], turn: int, refund: dict | None = None) -> dict:
    payload = {"items": [{"id": name, "label": ACTION_LABELS[name]} for name in actions if name in ACTION_LABELS],
               "turn": turn}
    if refund:
        payload["refund"] = refund
    return payload
