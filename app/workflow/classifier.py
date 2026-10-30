import asyncio
import json
import logging

from langchain_core.messages import HumanMessage, SystemMessage

from app.prompts import INTENT_SYSTEM
from app.schemas import IntentResult
from app.workflow.routing import DEFAULT_INTENT

logger = logging.getLogger(__name__)


class IntentClassifier:
    def __init__(self, model, *, timeout: float = 10.0):
        self.structured = model.with_structured_output(IntentResult, method="json_mode")
        self.timeout = timeout

    async def classify(self, question: str, previous_question: str | None = None) -> str:
        payload = json.dumps({"question": question, "previous_question": previous_question or ""},
                             ensure_ascii=False)
        try:
            async with asyncio.timeout(self.timeout):
                result = await self.structured.ainvoke(
                    [SystemMessage(content=INTENT_SYSTEM), HumanMessage(content=payload)]
                )
            if not isinstance(result, IntentResult):
                raise ValueError("Intent result has an unexpected type")
            return result.intent
        except Exception:
            logger.warning("意图分类失败，按默认意图 %s 继续。", DEFAULT_INTENT, exc_info=True)
            return DEFAULT_INTENT
