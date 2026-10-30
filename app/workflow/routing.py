import re

INTENTS = ("物流", "订单", "商品咨询", "退款退货", "售后", "投诉", "闲聊")
DEFAULT_INTENT = "售后"
ROUTES = {
    "商品咨询": "knowledge",
    "退款退货": "knowledge",
    "物流": "agent",
    "订单": "agent",
    "售后": "agent",
    "投诉": "complaint",
    "闲聊": "chitchat",
}

_CHITCHAT = re.compile(
    r"^(?:你好|您好|hi|hello|在吗|在不在|有人吗|哈喽|嗨|谢谢(?:你|您)?|多谢(?:你|您)?|感谢(?:你|您)?|谢啦|"
    r"再见|拜拜|byebye|晚安|早上好|中午好|下午好|晚上好|你是谁|你叫什么|你是机器人吗)"
    r"(?:呀|啊|哈|呢|哦|啦|嘛|吧|~|～)*$",
    re.IGNORECASE,
)


def route_for(intent: str) -> str:
    return ROUTES.get(intent, "agent")


def fast_chitchat(text: str) -> bool:
    cleaned = re.sub(r"[\s，。！？、,.!?~～…]+", "", text)
    return bool(cleaned) and len(cleaned) <= 12 and bool(_CHITCHAT.fullmatch(cleaned))
