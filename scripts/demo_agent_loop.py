"""裸 ReAct 循环演示：不经过 LangGraph，模型自己连续调用两个工具后收敛。"""

import asyncio

from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from app.agent.react import run_react
from app.config import Settings
from app.database import build_session_factory
from app.model import build_model
from app.models import Base
from app.prompts import chat_system
from app.storage import ConversationStore
from app.tools.wiring import build_engine, build_registry

QUESTION = "订单 1001 现在是什么状态？它的包裹到哪了？"


async def main() -> None:
    settings = Settings.from_env()
    model = build_model(settings)
    db_engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                              poolclass=StaticPool)
    Base.metadata.create_all(db_engine)
    store = ConversationStore(build_session_factory(db_engine))
    conversation_id = store.start_turn(user_id="demo", message=QUESTION)
    registry = build_registry(store, conversation_id, None, settings=settings)
    engine = build_engine(registry, settings=settings, session_factory=store.session_factory)

    messages = [SystemMessage(content=chat_system(settings.store_name)), HumanMessage(content=QUESTION)]
    print(f"用户：{QUESTION}\n")
    result = await run_react(
        model=model,
        registry=registry,
        engine=engine,
        messages=messages,
        on_token=lambda text: print(text, end="", flush=True),
        on_status=lambda payload: print(
            f"\n[{payload['phase']}] {payload['label']}"
            + (f" -> {payload.get('summary')}" if payload["phase"] == "end" else "")
        ),
        max_steps=settings.workflow_agent_max_steps,
        token_budget=settings.workflow_agent_token_budget,
        timeout=settings.request_timeout_seconds,
        store=store,
        conversation_id=conversation_id,
    )
    print(f"\n\n步骤={result.steps} 工具={result.tool_calls} 停止={result.stopped_by} 估算token={result.tokens}")

    await model.root_async_client.close()
    model.root_client.close()
    db_engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
