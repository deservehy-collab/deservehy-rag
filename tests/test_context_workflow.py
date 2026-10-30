import asyncio

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, SystemMessage
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select

from app.knowledge.retrieval import Evidence, RetrievalResult
from app.models import Message
from app.prompts import AGENT_ADDENDUM, KNOWLEDGE_HINT, POLICY_HINT, chat_system
from app.storage import ConversationStore
from app.workflow.graph import Workflow, WorkflowDeps
from tests.support import FixedClassifier, _settings, api, events, sqlite_factory, streamed


class Recorder:
    def __init__(self, rounds=None):
        self.rounds = list(rounds or [[AIMessageChunk(content="好的。",
                                                     response_metadata={"finish_reason": "stop"})]])
        self.seen = []

    def bind_tools(self, tools):
        return self

    async def astream(self, messages):
        self.seen.append(list(messages))
        for item in self.rounds.pop(0):
            yield item


class Retriever:
    def retrieve(self, query, category=None):
        return RetrievalResult("hybrid_rerank", query, [], [self._evidence()])

    def retrieve_expanded(self, queries):
        return RetrievalResult("hybrid_rerank", queries[0], queries=list(queries),
                               items=[self._evidence()], evidence=[self._evidence()])

    @staticmethod
    def _evidence():
        return Evidence(7, "退款时效", "退款审核通过后 1-3 个工作日到账。",
                        "售后政策", "售后政策 › 退款", "policy", {"rerank": 0.9}, 1)


class Expander:
    async def expand(self, question):
        return [question]


def ordered(text: str, *labels: str) -> bool:
    positions = [text.index(label) for label in labels]
    return positions == sorted(positions)


def build(factory, model, classifier):
    deps = WorkflowDeps(model=model, settings=_settings({}), store=ConversationStore(factory),
                        knowledge=Retriever(), pool=None, selfcheck=None,
                        classifier=FixedClassifier(classifier))
    workflow = Workflow(deps=deps, checkpointer=InMemorySaver())
    workflow.deps.expander = Expander()
    return workflow


def run(workflow, conversation_id, question):
    messages = [SystemMessage(content=chat_system("本店")), HumanMessage(content=question)]

    async def collect():
        return [item async for item in workflow.respond(conversation_id, question, messages)]
    return asyncio.run(collect())


def route_prompt(classifier, question):
    model = Recorder()
    with sqlite_factory() as factory:
        store = ConversationStore(factory)
        workflow = build(factory, model, classifier)
        conversation_id = store.start_turn(user_id="r1", message=question)
        run(workflow, conversation_id, question)
    assert model.seen, f"{classifier} 路由没有调用模型"
    return model.seen[0]


def test_system_is_identical_across_routes_and_hints_are_trailing():
    knowledge = route_prompt("商品咨询", "退款几天到账")
    policy = route_prompt("退款退货", "订单 1001 这个能退吗")
    agent = route_prompt("物流", "订单 1001 到哪了")

    # system 逐轮逐路由字节一致，上游才能命中前缀缓存。
    assert knowledge[0].content == policy[0].content == agent[0].content == chat_system("本店")
    assert [messages[0].type for messages in (knowledge, policy, agent)] == ["system"] * 3

    trailing = [messages[-1] for messages in (knowledge, policy, agent)]
    assert [item.type for item in trailing] == ["human"] * 3
    # 规格 §7：尾随块内部顺序固定为 证据 → 订单 → 本轮编排。
    assert ordered(trailing[0].content, "【知识库证据】", "【证据结束】", "【本轮编排】", KNOWLEDGE_HINT[:12])
    assert ordered(trailing[1].content, "【知识库证据】", "【证据结束】", "【订单信息】", "【本轮编排】")
    assert trailing[2].content.startswith("【本轮编排】")
    assert POLICY_HINT in trailing[1].content and "订单号：1001" in trailing[1].content
    assert AGENT_ADDENDUM in trailing[2].content
    assert "【知识库证据】" not in trailing[2].content

    current = [messages[-2] for messages in (knowledge, policy, agent)]
    assert [item.content for item in current] == ["退款几天到账", "订单 1001 这个能退吗", "订单 1001 到哪了"]
    assert all(item.type == "human" for item in current)


def test_upstream_payload_carries_three_layers_from_database():
    def long(text):
        return text * 60

    settings = {"context_window_tokens": 4000, "max_output_tokens": 200, "max_user_input_tokens": 200,
                "knowledge_top_k": 1, "workflow_agent_max_steps": 1, "tool_result_max_tokens": 100,
                "history_keep_rounds": 1, "history_round_tokens": 200}
    questions = ["第一问", "第二问", "第三问", "第四问"]
    answers = [long(text) for text in "甲乙丙丁"]
    calls = []

    def handler(request):
        calls.append(answers[len(calls)])
        return streamed((calls[-1],))

    with api(handler, **settings) as (client, seen):
        conversation_id = None
        for question in questions:
            payload = {"message": question}
            if conversation_id:
                payload["conversation_id"] = conversation_id
            conversation_id = events(client.post("/api/chat", json=payload))[0][1]["conversation_id"]

        messages = seen[3][1]["messages"]
        assert messages[0] == {"role": "system", "content": chat_system("本店")}
        # 客户端一次 history 都没回传，三层全部来自数据库。
        assert messages[1] == {"role": "user", "content": "第二问"}
        assert messages[2] == {"role": "assistant", "content": long("乙")[:40] + "…"}
        assert messages[3] == {"role": "user", "content": "第三问"}
        assert messages[4] == {"role": "assistant", "content": long("丙")}
        assert messages[-2] == {"role": "user", "content": "第四问"}
        assert AGENT_ADDENDUM in messages[-1]["content"]


def test_state_messages_merge_by_id_and_log_node_appends_reply():
    model = Recorder([[AIMessageChunk(content="第一答", response_metadata={"finish_reason": "stop"})],
                      [AIMessageChunk(content="第二答", response_metadata={"finish_reason": "stop"})]])
    with sqlite_factory() as factory:
        store = ConversationStore(factory)
        workflow = build(factory, model, "物流")
        conversation_id = store.start_turn(user_id="s1", message="第一问")

        def history():
            rows = store.context_messages(conversation_id)
            return [HumanMessage(content=row["content"], id=str(row["id"])) if row["role"] == "user"
                    else AIMessage(content=row["content"], id=str(row["id"])) for row in rows]

        async def respond(question):
            return [item async for item in workflow.respond(
                conversation_id, question, history())]

        asyncio.run(respond("第一问"))
        with factory() as session:
            rows = session.scalars(select(Message).order_by(Message.id)).all()
        assert [row.role for row in rows] == ["user", "assistant"]
        first_ids = [str(row.id) for row in rows]

        conversation_id = store.start_turn(user_id="s1", message="第二问",
                                          conversation_id=conversation_id)
        asyncio.run(respond("第二问"))

        async def read():
            return (await workflow.get_state(conversation_id)).values
        messages = asyncio.run(read())["messages"]
        ids = [item.id for item in messages]
        # 第二轮把数据库历史整份重投，add_messages 按 id 合并，不产生重复。
        assert len(ids) == len(set(ids))
        assert [item.type for item in messages] == ["human", "ai", "human", "ai"]
        assert [str(item.content) for item in messages] == ["第一问", "第一答", "第二问", "第二答"]
        assert ids[1] == first_ids[1]

        with factory() as session:
            rows = session.scalars(select(Message).order_by(Message.id)).all()
        assert [row.role for row in rows] == ["user", "assistant", "user", "assistant"]
        assert ids[-1] == str(rows[-1].id)
