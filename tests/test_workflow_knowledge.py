import asyncio
import time

from langchain_core.messages import AIMessageChunk, HumanMessage, SystemMessage
from langgraph.checkpoint.memory import InMemorySaver

from app.knowledge.retrieval import Evidence, KnowledgeRetriever, RetrievalResult
from app.prompts import chat_system
from app.storage import ConversationStore
from app.workflow.graph import Workflow, WorkflowDeps
from tests.support import FixedClassifier, _settings, sqlite_factory
from tests.test_retrieval_strategies import FakeEmbedder, FakeReranker, FakeStore, make_db


class FakeRetriever:
    def __init__(self, low=False):
        self.low = low
        self.calls = 0

    def retrieve(self, query):
        self.calls += 1
        item = Evidence(7, "G500 杯盖适配", "G500 适配 P1001 和 B500，不适用 B750。",
                        "型号与兼容性", "型号与兼容性 › 保温杯系列", "policy",
                        {"rerank": 0.92}, 1)
        # items 与 evidence 在生产里是两个不同列表：items 是重排序后的候选（未编号、n=0），
        # evidence 才是编号后注入 prompt 的那一份。假件必须把两者分开，否则「取错字段」也能全绿。
        decoy = Evidence(99, "重排候选：B750 杯盖价格", "重排候选占位原文，绝不该出现在证据里。",
                         "价格与优惠", "重排候选 › 保温杯系列", "policy",
                         {"rerank": 0.55}, 0)
        if self.low:
            return RetrievalResult("hybrid_rerank", query, low_confidence=True, reason="top1=0.1<0.30")
        return RetrievalResult("hybrid_rerank", query, [decoy], [item])


class Scripted:
    def __init__(self, text="G500 不适用 B750[1]。"):
        self.text = text
        self.calls = 0
        self.prompt = None

    def bind_tools(self, tools):
        self.tool_names = [tool.name for tool in tools]
        return self

    async def astream(self, messages):
        self.calls += 1
        self.prompt = messages[0].content
        yield AIMessageChunk(content=self.text, response_metadata={"finish_reason": "stop"})


class Pool:
    def __init__(self):
        self.rows = []

    def record(self, conversation_id, question, source, reason):
        self.rows.append((conversation_id, question, source, reason))


def build(factory, knowledge, pool=None, classifier="商品咨询"):
    store = ConversationStore(factory)
    model = Scripted()
    deps = WorkflowDeps(model=model, settings=_settings({}), store=store, knowledge=knowledge,
                        pool=pool, selfcheck=None, classifier=FixedClassifier(classifier))
    return store, Workflow(deps=deps, checkpointer=InMemorySaver()), model


def run(workflow, conversation_id, question):
    messages = [SystemMessage(content=chat_system("本店")), HumanMessage(content=question)]

    async def collect():
        return [item async for item in workflow.respond(conversation_id, question, messages)]
    return asyncio.run(collect())


def test_low_confidence_refuses_without_entering_agent():
    with sqlite_factory() as factory:
        store, workflow, model = build(factory, FakeRetriever(low=True), Pool())
        conversation_id = store.start_turn(user_id="k1", message="支持货到付款吗")
        frames = run(workflow, conversation_id, "支持货到付款吗")
        names = [name for name, _ in frames]
        assert names == ["status", "status", "token", "done"]
        assert frames[0][1]["tool"] == "retrieve"
        assert frames[2][1]["content"].startswith("这个问题我在店铺知识库里没有找到可靠依据")
        assert model.calls == 0
        assert workflow.deps.pool.rows[0][2:] == ("retrieval_low_conf", "top1=0.1<0.30")


def test_retrieval_failure_becomes_refusal():
    class Broken:
        def retrieve(self, query):
            raise RuntimeError("milvus down")

    with sqlite_factory() as factory:
        store, workflow, model = build(factory, Broken(), Pool())
        conversation_id = store.start_turn(user_id="k2", message="店铺政策是什么")
        frames = run(workflow, conversation_id, "店铺政策是什么")
        names = [name for name, _ in frames]
        assert names == ["status", "status", "token", "done"]
        assert frames[0][1]["tool"] == "retrieve"
        assert frames[2][1]["content"].startswith("这个问题我在店铺知识库里没有找到可靠依据")
        assert model.calls == 0
        assert frames[-1][0] == "done"
        assert workflow.deps.pool.rows[0][2:] == ("retrieval_low_conf", "retrieval_error")


def test_retrieval_timeout_becomes_refusal():
    class Slow:
        def retrieve(self, query):
            time.sleep(0.3)

    with sqlite_factory() as factory:
        store = ConversationStore(factory)
        deps = WorkflowDeps(model=Scripted(), settings=_settings({"knowledge_tool_timeout_seconds": 0.02}),
                            store=store, knowledge=Slow(), pool=Pool(), selfcheck=None,
                            classifier=FixedClassifier("商品咨询"))
        workflow = Workflow(deps=deps, checkpointer=InMemorySaver())
        conversation_id = store.start_turn(user_id="k9", message="店铺政策是什么")
        frames = run(workflow, conversation_id, "店铺政策是什么")
        assert [name for name, _ in frames] == ["status", "status", "token", "done"]
        assert frames[1][1]["summary"] == "知识库响应超时"
        assert frames[2][1]["content"].startswith("这个问题我在店铺知识库里没有找到可靠依据")
        assert workflow.deps.model.calls == 0
        assert workflow.deps.pool.rows[0][2:] == ("retrieval_low_conf", "retrieval_timeout")


def test_gate_pass_injects_evidence_and_drops_query_faq_tool():
    with sqlite_factory() as factory:
        store, workflow, model = build(factory, FakeRetriever(), Pool())
        conversation_id = store.start_turn(user_id="k3", message="G500 能装 B750 吗")
        frames = run(workflow, conversation_id, "G500 能装 B750 吗")
        names = [name for name, _ in frames]
        assert names == ["status", "citations", "status", "token", "done"]
        assert frames[0][1]["phase"] == "start"
        assert frames[0][1]["tool"] == "retrieve"
        assert frames[1][0] == "citations"
        assert frames[2][1]["phase"] == "end"
        assert frames[2][1]["tool"] == "retrieve"
        assert frames[1][1]["items"][0]["chunk_id"] == 7
        assert frames[1][1]["items"][0]["n"] == 1
        assert workflow.deps.knowledge.calls == 1
        assert "query_faq" not in model.tool_names
        assert "query_order" in model.tool_names
        assert model.calls == 1
        # 证据进 prompt 是 ch05 的核心支柱：prompt 必须是编号证据那一份（evidence），
        # 而不是重排候选（items）——decoy 文本出现在 prompt 里就说明取错了字段。
        assert "【知识库证据】" in model.prompt
        assert "G500 适配 P1001 和 B500，不适用 B750。" in model.prompt
        assert "本轮店铺知识库已经替你检索完毕" in model.prompt
        assert "重排候选" not in model.prompt


def test_retriever_gate_contract_never_pass_without_evidence(tmp_path):
    factory = make_db(tmp_path)
    ranked = FakeStore()
    ranked.hybrid = [("2", 0.03), ("1", 0.02), ("3", 0.01)]
    passing = KnowledgeRetriever(factory, FakeEmbedder(), ranked, candidates=3, top_k=3,
                                 reranker=FakeReranker([0.5, 0.9, 0.6]), rerank_min_score=0.3)
    result = passing.retrieve("问题", canonical="规范问题")
    assert result.low_confidence is False
    assert result.evidence
    assert all(isinstance(item.n, int) and item.n > 0 for item in result.evidence)

    weak = FakeStore()
    weak.hybrid = [("1", 0.03)]
    below = KnowledgeRetriever(factory, FakeEmbedder(), weak, candidates=3, top_k=3,
                               reranker=FakeReranker([0.1]), rerank_min_score=0.3)
    top1_low = below.retrieve("问题", canonical="规范")
    assert top1_low.low_confidence is True
    assert top1_low.evidence == []
    assert top1_low.reason.startswith("top1=")

    empty = KnowledgeRetriever(factory, FakeEmbedder(), FakeStore(), candidates=3, top_k=3,
                               reranker=FakeReranker([0.9]), rerank_min_score=0.3)
    no_hits = empty.retrieve("问题", canonical="规范")
    assert no_hits.low_confidence is True and no_hits.reason == "no_hits"
    blank = empty.retrieve("   ")
    assert blank.low_confidence is True and blank.reason == "empty_query"

    unnumbered = FakeStore()
    unnumbered.dense = [("1", 0.8)]
    dense_store = KnowledgeRetriever(factory, FakeEmbedder(), unnumbered, candidates=3, top_k=3,
                                     strategy="dense")
    dense = dense_store.retrieve("q", strategy="dense", canonical="q")
    assert dense.low_confidence is False

    for outcome in (result, top1_low, no_hits, blank, dense):
        if not outcome.low_confidence:
            assert outcome.evidence
