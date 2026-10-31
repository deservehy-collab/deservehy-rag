import logging

from langgraph.config import get_stream_writer

logger = logging.getLogger(__name__)

# 图可能停在 interrupt 挂起：纯 custom 流看不见那一帧，服务层必须双模式。
CUSTOM_UPDATES = ["custom", "updates"]


def unwrap(mode: str, data: object) -> list[tuple[str, dict]]:
    """把双模式流的一帧翻译成对外事件。

    custom：节点自己发的 (event, payload)，原样透出。
    updates：只关心 interrupt——图挂起时外部拿不到任何 custom 帧，
    这里翻成 confirm＋done，前端才知道要出卡片、并且可以收尾这条流。
    """
    if mode == "custom":
        return [data]
    payload = data if isinstance(data, dict) else {}
    interrupted = payload.get("__interrupt__")
    if not interrupted:
        return []
    # 一次只有一个 interrupt 节点（confirm_ticket），所以取第 0 个就是这张卡片。
    value = getattr(interrupted[0], "value", None) if isinstance(interrupted, (list, tuple)) else None
    if value is None:
        # 认不出的挂起形状（LangGraph 改了 Interrupt 结构）也不能发空白卡片，只收尾这条流。
        logger.warning("interrupt 帧里没有卡片载荷，只发 done 收尾：%r", interrupted)
        return [("done", {"finish_reason": "stop"})]
    return [("confirm", value), ("done", {"finish_reason": "stop"})]


def emit(event: str, payload: dict) -> None:
    """在图内把 (event, payload) 写进自定义流；图外单测直接静默。"""
    try:
        writer = get_stream_writer()
    except RuntimeError:
        return
    writer((event, payload))
