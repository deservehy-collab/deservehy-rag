import asyncio

from langchain_core.messages import AIMessageChunk, HumanMessage, SystemMessage
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select

from app.agent.guards import FALLBACK_PLAIN
from app.models import Message
from app.prompts import CHITCHAT_TEXT, COMPLAINT_TEXT, chat_system
from app.storage import ConversationStore
from app.workflow.graph import Workflow, WorkflowDeps
from tests.support import FixedClassifier, _settings, sqlite_factory


class NoModel:
    def bind_tools(self, tools):
        raise AssertionError("固定话术不应调用模型")

    async def astream(self, messages):
        raise AssertionError("固定话术不应调用模型")


class EmptyModel:
    def bind_tools(self, tools):
        return self

    async def astream(self, messages):
        yield AIMessageChunk(content="", response_metadata={"finish_reason": "stop"})


class ExplodingClassifier:
    async def classify(self, question, previous_question=None):
        raise AssertionError("快速通道不应调用分类器")


class CountingClassifier:
    def __init__(self, intent):
        self.intent = intent
        self.calls = 0

    async def classify(self, question, previous_question=None):
        self.calls += 1
        return self.intent


def build(factory, classifier, model=None):
    deps = WorkflowDeps(model=model or NoModel(), settings=_settings({}), store=ConversationStore(factory),
                        knowledge=None, pool=None, selfcheck=None, classifier=classifier)
    return Workflow(deps=deps, checkpointer=InMemorySaver())


def messages(question):
    return [SystemMessage(content=chat_system("本店")), HumanMessage(content=question)]


def run(workflow, conversation_id, question):
    async def collect():
        return [item async for item in workflow.respond(conversation_id, question, messages(question))]
    return asyncio.run(collect())


def state(workflow, conversation_id):
    async def read():
        return (await workflow.get_state(conversation_id)).values
    return asyncio.run(read())


def test_complaint_emits_two_independent_actions_and_persists_offer():
    with sqlite_factory() as factory:
        store = ConversationStore(factory)
        classifier = CountingClassifier("投诉")
        workflow = build(factory, classifier)
        conversation_id = store.start_turn(user_id="u1", message="我要投诉")
        frames = run(workflow, conversation_id, "我要投诉")
        assert [name for name, _ in frames] == ["token", "actions", "done"]
        # 投诉节点本身零生成调用（NoModel 会抛错），但意图分类仍恰好调用一次。
        assert classifier.calls == 1
        assert frames[0][1] == {"content": COMPLAINT_TEXT}
        actions = frames[1][1]
        assert [item["id"] for item in actions["items"]] == ["human", "ticket"]
        assert actions["turn"] == 1

        values = state(workflow, conversation_id)
        assert values["offer"]["actions"] == ["human", "ticket"]
        assert values["offer"]["ticket"]["type"] == "投诉"
        assert values["turn_count"] == 1

        with factory() as session:
            rows = session.scalars(select(Message)).all()
            assert rows[-1].content == COMPLAINT_TEXT


def test_chitchat_fast_path_never_touches_model_or_classifier():
    with sqlite_factory() as factory:
        store = ConversationStore(factory)
        workflow = build(factory, ExplodingClassifier())
        conversation_id = store.start_turn(user_id="u2", message="你好")
        frames = run(workflow, conversation_id, "你好")
        assert frames == [("token", {"content": CHITCHAT_TEXT}), ("done", {"finish_reason": "stop"})]


def test_turn_count_accumulates_and_offer_is_cleared_each_turn():
    with sqlite_factory() as factory:
        store = ConversationStore(factory)
        workflow = build(factory, FixedClassifier("投诉"))
        conversation_id = store.start_turn(user_id="u3", message="我要投诉")
        run(workflow, conversation_id, "我要投诉")

        async def second():
            return [item async for item in workflow.respond(conversation_id, "你好", messages("你好"))]
        frames = asyncio.run(second())
        assert frames[0][1]["content"] == CHITCHAT_TEXT

        values = state(workflow, conversation_id)
        assert values["turn_count"] == 2
        assert values["offer"] is None
        assert values["intent"] == "闲聊"


def test_empty_reply_uses_fallback_plain():
    with sqlite_factory() as factory:
        store = ConversationStore(factory)
        workflow = build(factory, FixedClassifier("售后"), EmptyModel())
        conversation_id = store.start_turn(user_id="u4", message="随便")
        frames = run(workflow, conversation_id, "随便")
        assert frames == [("token", {"content": FALLBACK_PLAIN}), ("done", {"finish_reason": "stop"})]
