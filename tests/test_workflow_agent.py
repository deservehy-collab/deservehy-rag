import asyncio

from langchain_core.messages import AIMessageChunk, HumanMessage, SystemMessage
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select

from app.knowledge.retrieval import Evidence, RetrievalResult
from app.models import Message
from app.prompts import chat_system
from app.storage import ConversationStore
from app.workflow.graph import Workflow, WorkflowDeps
from tests.support import FakeMcp, FixedClassifier, _settings, mcp_logistics_spec, sqlite_factory


class FakeRetriever:
    """items 与 evidence 必须是两份可区分的列表：生产里 retrieve_node 与 query_faq 都只读 evidence（任务 7 的教训）。"""

    def retrieve(self, query, category=None):
        item = Evidence(7, "退款时效", "退款审核通过后 1-3 个工作日到账。",
                        "售后政策", "售后政策 › 退款", "policy", {"rerank": 0.9}, 1)
        decoy = Evidence(99, "重排候选：退款到账银行", "重排候选原文，不该进 prompt。",
                         "售后政策", "售后政策 › 重排候选", "policy", {"rerank": 0.4}, 0)
        return RetrievalResult("hybrid_rerank", query, [decoy], [item])


class LowRetriever:
    def retrieve(self, query, category=None):
        return RetrievalResult("hybrid_rerank", query, low_confidence=True, reason="top1=0.1<0.30")


class MultiRound:
    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.calls = 0
        self.tool_names = []
        self.prompt = ""
        self.questions = []
        self.shapes = []

    def bind_tools(self, tools):
        self.tool_names = [tool.name for tool in tools]
        return self

    async def astream(self, messages):
        self.calls += 1
        self.messages = messages
        # system 现在逐轮字节一致，路由提示与证据挪到最后一条 user 块，断言看拼接后的全文。
        self.prompt = "\n".join(str(item.content) for item in messages)
        self.questions = [str(item.content) for item in messages if item.type == "human"]
        self.shapes.append([type(item).__name__ for item in messages])
        for item in self.rounds.pop(0):
            yield item


def chunk(text="", finish="stop", tool_calls=None):
    return AIMessageChunk(content=text, tool_calls=tool_calls or [],
                          response_metadata={"finish_reason": finish})


def call(name, args, call_id="call_1"):
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


class Pool:
    def __init__(self):
        self.rows = []

    def record(self, conversation_id, question, source, reason):
        self.rows.append((conversation_id, source, reason))


class SelfCheck:
    def __init__(self, useful):
        self.useful = useful

    def check(self, question, canonical, items):
        return self.useful, "证据没有到账时间"


def build(factory, model, knowledge=None, pool=None, selfcheck=None, classifier="物流", mcp=None):
    deps = WorkflowDeps(model=model, settings=_settings({}), store=ConversationStore(factory),
                        knowledge=knowledge, pool=pool, selfcheck=selfcheck,
                        classifier=FixedClassifier(classifier), mcp=mcp)
    return Workflow(deps=deps, checkpointer=InMemorySaver())


def run(workflow, conversation_id, question):
    messages = [SystemMessage(content="base system"), HumanMessage(content=question)]

    async def collect():
        return [item async for item in workflow.respond(conversation_id, question, messages)]
    return asyncio.run(collect())


def test_business_route_keeps_all_tools_and_no_evidence_block():
    model = MultiRound([[chunk("包裹在上海。")]])
    with sqlite_factory() as factory:
        store = ConversationStore(factory)
        conversation_id = store.start_turn(user_id="a1", message="订单 1001 的物流到哪了")
        workflow = build(factory, model)
        frames = run(workflow, conversation_id, "订单 1001 的物流到哪了")
        assert model.tool_names == ["query_order", "query_product", "query_faq", "create_ticket"]
        assert [name for name, _ in frames] == ["token", "done"]
        assert "【知识库证据】" not in model.prompt
        assert "本轮由工作流编排" in model.prompt


def test_mcp_specs_join_the_bound_tool_list():
    """MCP 清单接上就与内置合成一份表：模型这轮能看见的业务工具是五个。"""
    model = MultiRound([[chunk("包裹在上海。")]])
    with sqlite_factory() as factory:
        store = ConversationStore(factory)
        conversation_id = store.start_turn(user_id="a1b", message="订单 1001 的物流到哪了")
        workflow = build(factory, model, mcp=FakeMcp([mcp_logistics_spec()]))
        frames = run(workflow, conversation_id, "订单 1001 的物流到哪了")
        assert set(model.tool_names) == {"query_order", "query_product", "query_logistics",
                                         "query_faq", "create_ticket"}
        assert len(model.tool_names) == 5
        assert [name for name, _ in frames][-1] == "done"


def test_actions_marker_is_stripped_and_event_has_turn():
    model = MultiRound([[chunk("可以帮你转人工。[[actions:human,ticket]]")]])
    with sqlite_factory() as factory:
        store = ConversationStore(factory)
        conversation_id = store.start_turn(user_id="a2", message="订单有问题")
        workflow = build(factory, model, classifier="订单")
        frames = run(workflow, conversation_id, "订单有问题")
        assert ("token", {"content": "可以帮你转人工。"}) in frames
        actions = [payload for name, payload in frames if name == "actions"][0]
        assert actions == {"items": [{"id": "human", "label": "转人工"},
                                     {"id": "ticket", "label": "建工单"}], "turn": 1}
        assert [name for name, _ in frames][-1] == "done"

        async def state():
            return (await workflow.get_state(conversation_id)).values
        values = asyncio.run(state())
        assert values["offer"]["actions"] == ["human", "ticket"]
        assert values["offer"]["ticket"]["type"] == "咨询"


def test_query_faq_low_confidence_refuses_inside_business_route():
    model = MultiRound([[chunk("", "tool_calls", [call("query_faq", {"keyword": "退款"})])],
                        [chunk("不该出现的第二轮")]])
    pool = Pool()
    with sqlite_factory() as factory:
        store = ConversationStore(factory)
        conversation_id = store.start_turn(user_id="a3", message="退款几天到账")
        workflow = build(factory, model, knowledge=LowRetriever(), pool=pool)
        frames = run(workflow, conversation_id, "退款几天到账")
        assert frames[-2][1]["content"].startswith("这个问题我在店铺知识库里没有找到可靠依据")
        assert model.calls == 1
        assert pool.rows[0][1:] == ("retrieval_low_conf", "top1=0.1<0.30")


def test_query_faq_self_check_rejection_stops_generation():
    model = MultiRound([[chunk("", "tool_calls", [call("query_faq", {"keyword": "退款"})])],
                        [chunk("不该出现的第二轮")]])
    pool = Pool()
    with sqlite_factory() as factory:
        store = ConversationStore(factory)
        conversation_id = store.start_turn(user_id="a4", message="退款几天到账")
        workflow = build(factory, model, knowledge=FakeRetriever(), pool=pool,
                         selfcheck=SelfCheck(False))
        frames = run(workflow, conversation_id, "退款几天到账")
        assert frames[-2][1]["content"].startswith("这个问题我在店铺知识库里没有找到可靠依据")
        assert model.calls == 1
        assert pool.rows[0][1] == "self_check"


def test_knowledge_route_citation_guard_keeps_only_injected_evidence_numbers():
    model = MultiRound([[chunk("退款 1-3 个工作日到账[1]。普通地区满 99 元包邮[9]。")]])
    with sqlite_factory() as factory:
        store = ConversationStore(factory)
        conversation_id = store.start_turn(user_id="a5", message="退款几天到账")
        workflow = build(factory, model, knowledge=FakeRetriever(), pool=Pool(), classifier="商品咨询")
        frames = run(workflow, conversation_id, "退款几天到账")
        assert [name for name, _ in frames] == ["status", "citations", "status", "token", "done"]
        assert "[1] 小节：售后政策 › 退款" in model.prompt
        assert "重排候选" not in model.prompt
        assert frames[1][1]["items"][0]["n"] == 1 and frames[1][1]["items"][0]["chunk_id"] == 7
        assert frames[3][1]["content"] == "退款 1-3 个工作日到账[1]。普通地区满 99 元包邮。"


def test_agent_route_multi_round_persists_tool_round_and_one_reply_row():
    model = MultiRound([
        [chunk("我先查一下。", "tool_calls", [call("query_order", {"order_id": "1001"})])],
        [chunk("订单已发货，包裹正在运输途中。")],
    ])
    with sqlite_factory() as factory:
        store = ConversationStore(factory)
        conversation_id = store.start_turn(user_id="a6", message="订单 1001 到哪了")
        workflow = build(factory, model, classifier="物流")
        frames = run(workflow, conversation_id, "订单 1001 到哪了")
        assert [name for name, _ in frames] == ["token", "status", "status", "token", "done"]
        assert model.calls == 2
        assert model.shapes[-1][-2:] == ["AIMessage", "ToolMessage"]
        streamed = "".join(payload["content"] for name, payload in frames if name == "token")
        assert streamed == "我先查一下。订单已发货，包裹正在运输途中。"

        async def state():
            return (await workflow.get_state(conversation_id)).values
        values = asyncio.run(state())
        assert values["reply"] == streamed
        assert values["tool_calls"] == 1 and values["steps"] == 2 and values["stopped_by"] is None

        with factory() as session:
            rows = session.scalars(select(Message).order_by(Message.id)).all()
        assistant = [row for row in rows if row.role == "assistant"]
        assert [row.content for row in assistant if row.content] == [streamed]
        assert any(row.tool_calls and row.content is None for row in assistant)
        assert [row.role for row in rows] == ["user", "assistant", "tool", "assistant"]


class AsyncReturn:
    def __init__(self, value):
        self.value = value

    async def expand(self, question):
        return list(self.value)


class ExpandedRetriever:
    def retrieve_expanded(self, queries):
        item = Evidence(7, "退款时效", "退款审核通过后 1-3 个工作日到账。",
                        "售后政策", "售后政策 › 退款", "policy", {"rerank": 0.9}, 1)
        return RetrievalResult("hybrid_rerank", queries[0], queries=list(queries),
                               items=[item], evidence=[item])


def latest_state(workflow, conversation_id):
    async def read():
        return (await workflow.get_state(conversation_id)).values
    return asyncio.run(read())


def test_policy_route_uses_order_and_evidence_and_drops_query_faq():
    model = MultiRound([[chunk("这一单在七天无理由范围内，可以在订单页提交退款。[[actions:refund]]")]])
    with sqlite_factory() as factory:
        store = ConversationStore(factory)
        conversation_id = store.start_turn(user_id="p1", message="这个能退吗")
        workflow = build(factory, model, knowledge=ExpandedRetriever(), classifier="退款退货")
        workflow.deps.expander = AsyncReturn(["订单 1001 这个能退吗", "退款政策"])
        frames = run(workflow, conversation_id, "订单 1001 这个能退吗")
        assert "query_faq" not in model.tool_names
        assert "【订单信息】" in model.prompt and "订单号：1001" in model.prompt
        assert "【知识库证据】" in model.prompt
        actions = [payload for name, payload in frames if name == "actions"][0]
        assert actions["items"] == [{"id": "refund", "label": "申请退款"}]
        assert actions["refund"]["order_id"] == "1001"
        values = latest_state(workflow, conversation_id)
        assert values["offer"]["actions"] == ["refund"]
        assert values["offer"]["refund"]["order_id"] == "1001"


def test_refund_action_dropped_when_no_order():
    model = MultiRound([[chunk("先帮你转人工。[[actions:refund,human]]")]])
    with sqlite_factory() as factory:
        store = ConversationStore(factory)
        conversation_id = store.start_turn(user_id="p2", message="订单有问题")
        workflow = build(factory, model, classifier="订单")
        frames = run(workflow, conversation_id, "订单有问题")
        actions = [payload for name, payload in frames if name == "actions"][0]
        assert [item["id"] for item in actions["items"]] == ["human"]
        assert "refund" not in actions


class FixedResolver:
    def __init__(self, question):
        self.question = question

    async def resolve(self, question, **kwargs):
        return self.question


def test_agent_sees_resolved_question():
    model = MultiRound([[chunk("这一单可以退。[[actions:refund]]")]])
    with sqlite_factory() as factory:
        store = ConversationStore(factory)
        conversation_id = store.start_turn(user_id="p3", message="订单 1001")
        workflow = build(factory, model, knowledge=ExpandedRetriever(), classifier="退款退货")
        workflow.deps.resolver = FixedResolver("订单 1001 的商品能退吗")
        run(workflow, conversation_id, "订单 1001")
        assert "订单 1001 的商品能退吗" in model.questions
        # 指代消解的结果只替换当前这句话，system 仍是逐轮一致的人设与红线。
        assert model.messages[0].content == chat_system("本店")
