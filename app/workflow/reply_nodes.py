import asyncio
import logging
import time

from langchain_core.messages import AIMessage

from app.agent.guards import FALLBACK_PLAIN
from app.prompts import CHITCHAT_TEXT, COMPLAINT_TEXT
from app.workflow.actions import actions_payload, build_offer
from app.workflow.events import emit

logger = logging.getLogger(__name__)


def complaint_node(state) -> dict:
    turn = state.get("turn_count") or 1
    actions = ["human", "ticket"]
    emit("token", {"content": COMPLAINT_TEXT})
    emit("actions", actions_payload(actions, turn))
    return {"reply": COMPLAINT_TEXT, "actions": actions,
            "offer": build_offer(turn, actions, state["question"], "投诉"),
            "finish_reason": "stop"}


def chitchat_node(state) -> dict:
    emit("token", {"content": CHITCHAT_TEXT})
    return {"reply": CHITCHAT_TEXT, "actions": [], "offer": None, "finish_reason": "stop"}


def make_log_node(deps):
    async def log_node(state) -> dict:
        conversation_id = state["conversation_id"]
        reply = state.get("reply") or FALLBACK_PLAIN
        elapsed = time.time() - state.get("started_at", time.time())
        retrieval = state.get("retrieval") or {}
        logger.info(
            "workflow node=log conv=%s intent=%s route=%s low_conf=%s steps=%s tools=%s tokens=%s "
            "stopped=%s elapsed=%.2fs",
            conversation_id, state.get("intent"), state.get("route"),
            bool(retrieval.get("low_confidence")), state.get("steps"), state.get("tool_calls"),
            state.get("tokens"), state.get("stopped_by"), elapsed,
        )
        message_id = await asyncio.to_thread(deps.store.add_message, conversation_id, "assistant", content=reply)
        emit("done", {"finish_reason": state.get("finish_reason") or "stop"})
        return {"messages": [AIMessage(content=reply, id=str(message_id))]}

    return log_node
