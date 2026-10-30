from app.agent.guards import ACTION_LABELS

TICKET_TYPES = {"退款退货": "售后", "售后": "售后", "投诉": "投诉"}


def offer_ticket_type(intent: str) -> str:
    return TICKET_TYPES.get(intent, "咨询")


def build_offer(turn: int, actions: list[str], question: str, intent: str) -> dict:
    ticket = None
    if "ticket" in actions:
        kind = offer_ticket_type(intent)
        prefix = "用户投诉" if kind == "投诉" else "用户反馈"
        ticket = {"type": kind, "description": f"{prefix}：{question}"[:500]}
    return {"turn": turn, "actions": list(actions), "ticket": ticket, "ticket_no": None}


def actions_payload(actions: list[str], turn: int) -> dict:
    return {"items": [{"id": name, "label": ACTION_LABELS[name]} for name in actions if name in ACTION_LABELS],
            "turn": turn}
