"""语义查重：只在「待审」里找同类。返回 None = 这是新缺口。"""

import logging

from langchain_core.messages import HumanMessage, SystemMessage

from app.flywheel.prompts import DEDUPE_SYSTEM
from app.knowledge.llm_json import extract_json_object

logger = logging.getLogger(__name__)

MAX_CANDIDATES = 20


async def find_match(model, question: str, candidates) -> int | None:
    """candidates 是 [(review_id, normalized_question)]。模型返回的 id 不在候选里一律作废。"""
    try:
        # 候选 id 解析也放在 try 里：库里出现非法 id 时按「查重失败 → 新缺口」处理，不能抛给调用方。
        allowed = [(int(review_id), str(text)) for review_id, text in (candidates or [])][:MAX_CANDIDATES]
        if not allowed:
            return None
        prompt = ("待判问题：" + str(question) + "\n候选：\n"
                  + "\n".join(f"- id={review_id} {text}" for review_id, text in allowed))
        reply = await model.ainvoke([SystemMessage(content=DEDUPE_SYSTEM),
                                     HumanMessage(content=prompt)])
        raw = extract_json_object(reply.content).get("matched_id")
        # bool 是 int 的子类：true/false 会被 int() 成 1/0，必须显式当作无效。
        matched = int(raw) if raw is not None and not isinstance(raw, bool) else None
    except Exception:
        logger.warning("查重失败，按新缺口处理。", exc_info=True)
        return None
    if matched is None or matched not in {review_id for review_id, _ in allowed}:
        return None
    return matched
