from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from app.schemas import ChatRequest


class ContextBudgetError(ValueError):
    pass


def estimate_tokens(messages: list[BaseMessage]) -> int:
    return 8 + sum(len(message.content.encode("utf-8")) + 16 for message in messages)


def build_messages(system: str, request: ChatRequest, budget: int) -> list[BaseMessage]:
    messages = [SystemMessage(content=system)]
    messages.extend(
        HumanMessage(content=item.content) if item.role == "user" else AIMessage(content=item.content)
        for item in request.history
    )
    messages.append(HumanMessage(content=request.message))
    while estimate_tokens(messages) > budget and len(messages) > 2:
        del messages[1:3]
    if estimate_tokens(messages) > budget:
        raise ContextBudgetError("当前问题超过上下文预算，请缩短后重试。")
    return messages
