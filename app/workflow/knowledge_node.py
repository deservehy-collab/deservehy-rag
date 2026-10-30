import asyncio
import logging
import time

from app.knowledge.quality import REFUSAL_TEXT
from app.workflow.events import emit

logger = logging.getLogger(__name__)


async def record_low_confidence(pool, conversation_id, question, source, reason):
    if pool is None:
        return
    try:
        await asyncio.to_thread(pool.record, conversation_id, question, source, reason)
    except Exception:
        logger.exception("低置信问题入池失败，不影响回复。")


def make_retrieve_node(deps):
    async def retrieve_node(state) -> dict:
        started = time.perf_counter()
        emit("status", {"phase": "start", "tool": "retrieve", "label": "检索知识库"})
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(deps.knowledge.retrieve, state["question"]),
                deps.settings.knowledge_tool_timeout_seconds,
            )
        except TimeoutError:
            logger.warning("强制检索超时（%s 秒），按低置信处理。", deps.settings.knowledge_tool_timeout_seconds)
            emit("status", {"phase": "end", "tool": "retrieve", "label": "检索知识库", "ok": False,
                            "summary": "知识库响应超时",
                            "duration_ms": int((time.perf_counter() - started) * 1000)})
            return {"retrieval": {"low_confidence": True, "reason": "retrieval_timeout",
                                  "canonical_query": "", "strategy": ""},
                    "evidence": [], "citations": []}
        except Exception:
            logger.exception("强制检索失败，按低置信处理。")
            emit("status", {"phase": "end", "tool": "retrieve", "label": "检索知识库", "ok": False,
                            "summary": "知识库暂时不可用",
                            "duration_ms": int((time.perf_counter() - started) * 1000)})
            return {"retrieval": {"low_confidence": True, "reason": "retrieval_error",
                                  "canonical_query": "", "strategy": ""},
                    "evidence": [], "citations": []}

        evidence = [item.item() for item in result.evidence]
        citations = [item.citation() for item in result.evidence]
        if citations:
            emit("citations", {"items": citations})
        emit("status", {
            "phase": "end", "tool": "retrieve", "label": "检索知识库",
            "ok": not result.low_confidence,
            "summary": f"命中 {len(evidence)} 条知识" if evidence else (result.reason or "没有命中"),
            "duration_ms": int((time.perf_counter() - started) * 1000),
        })
        return {"retrieval": {"low_confidence": result.low_confidence, "reason": result.reason,
                              "canonical_query": result.canonical_query, "strategy": result.strategy},
                "evidence": evidence, "citations": citations}

    return retrieve_node


def gate_node(state) -> dict:
    return {}


def gate_selector(state) -> str:
    retrieval = state.get("retrieval") or {}
    return "refuse" if retrieval.get("low_confidence", True) else "agent"


def make_refuse_node(deps):
    async def refuse_node(state) -> dict:
        reason = (state.get("retrieval") or {}).get("reason") or "low_confidence"
        await record_low_confidence(deps.pool, state["conversation_id"], state["question"],
                                    "retrieval_low_conf", reason)
        emit("token", {"content": REFUSAL_TEXT})
        return {"reply": REFUSAL_TEXT, "actions": [], "offer": None, "finish_reason": "stop"}

    return refuse_node
