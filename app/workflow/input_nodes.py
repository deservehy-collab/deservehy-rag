import time

from app.workflow.routing import fast_chitchat, route_for


def coref_node(state) -> dict:
    return {
        "previous_question": state.get("last_question"),
        "turn_count": 1,
        "last_question": state["question"],
        "actions": [],
        "offer": None,
        "retrieval": {},
        "evidence": [],
        "citations": [],
        "reply": "",
        "finish_reason": None,
        "steps": 0,
        "tool_calls": 0,
        "tokens": 0,
        "stopped_by": None,
        "started_at": time.time(),
    }


def make_intent_node(deps):
    async def intent_node(state) -> dict:
        question = state["question"]
        if fast_chitchat(question):
            intent = "闲聊"
        else:
            intent = await deps.classifier.classify(question, state.get("previous_question"))
        return {"intent": intent, "last_intent": intent}

    return intent_node


def route_node(state) -> dict:
    return {"route": route_for(state.get("intent") or "")}


def route_selector(state) -> str:
    return state.get("route") or "agent"
