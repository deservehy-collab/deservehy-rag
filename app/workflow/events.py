import logging

from langgraph.config import get_stream_writer

logger = logging.getLogger(__name__)


def emit(event: str, payload: dict) -> None:
    """在图内把 (event, payload) 写进自定义流；图外单测直接静默。"""
    try:
        writer = get_stream_writer()
    except RuntimeError:
        return
    writer((event, payload))
