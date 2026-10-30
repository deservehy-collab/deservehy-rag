import logging

from app.schemas import IntentResult
from app.workflow.routing import fast_chitchat, route_for

logger = logging.getLogger(__name__)


def make_intent_node(deps):
    async def intent_node(state) -> dict:
        question = state["question"]
        if fast_chitchat(question):
            result = IntentResult(intent="闲聊", confidence=1.0)
        else:
            result = await deps.classifier.classify(
                state.get("resolved_question") or question, state.get("previous_question"))
        intent, confidence = result.intent, float(result.confidence)
        if confidence < deps.settings.workflow_intent_min_confidence:
            logger.info("意图置信度 %.2f 低于阈值，按其他处理：%s", confidence, intent)
            intent = "其他"
        return {"intent": intent, "intent_confidence": confidence, "last_intent": intent}

    return intent_node


def route_node(state) -> dict:
    return {"route": route_for(state.get("intent") or "")}


def route_selector(state) -> str:
    return state.get("route") or "agent"
