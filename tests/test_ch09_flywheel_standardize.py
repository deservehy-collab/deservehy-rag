import asyncio
import json

from app.flywheel.dedupe import find_match
from app.flywheel.standardize import Standardized, standardize_batch
from tests.support import msg_chunk, ScriptedModel


def fake_reply(payload):
    return ScriptedModel([[msg_chunk(json.dumps(payload, ensure_ascii=False))]])


def test_batch_splits_by_batch_size():
    model = ScriptedModel([
        [msg_chunk(json.dumps({"items": [
            {"pool_id": 1, "normalized_question": "运费标准", "sample_answer": "满99免运费"},
            {"pool_id": 2, "normalized_question": "退货流程", "sample_answer": None}]},
            ensure_ascii=False))],
        [msg_chunk(json.dumps({"items": [
            {"pool_id": 3, "normalized_question": "发票开具", "sample_answer": None}]},
            ensure_ascii=False))],
    ])
    rows = [(1, "邮费多少"), (2, "怎么退货"), (3, "发票怎么开")]
    got = asyncio.run(standardize_batch(model, rows, batch_size=2))
    assert [item.pool_id for item in got] == [1, 2, 3]
    assert isinstance(got[0], Standardized) and got[0].sample_answer == "满99免运费"
    assert model.calls == 2      # 3 条按 2 一批：真按 batch_size 切了才会调两次


def test_unparsable_batch_degrades_per_row_not_whole_batch():
    model = ScriptedModel([[msg_chunk("我觉得标准问是运费标准")]])   # 不是 JSON
    rows = [(1, "邮费多少"), (2, "怎么退货")]
    got = asyncio.run(standardize_batch(model, rows, batch_size=10))
    assert [item.normalized_question for item in got] == ["邮费多少", "怎么退货"]
    assert all(item.sample_answer is None for item in got)


def test_model_exception_degrades_to_raw_question():
    # 注意别写成 [[RuntimeError(...)]]：ScriptedModel._next 只在「轮次元素本身就是 Exception」时 raise，
    # 包进 list 会被 ainvoke 当成 content 为空的片段，测到的是 JSON 解析失败而不是异常路径。
    model = ScriptedModel([RuntimeError("upstream 500")])
    got = asyncio.run(standardize_batch(model, [(7, "邮费多少")], batch_size=10))
    assert got == [Standardized(7, "邮费多少", None)]


def test_ids_not_in_batch_are_dropped():
    model = fake_reply({"items": [{"pool_id": 999, "normalized_question": "x", "sample_answer": None}]})
    got = asyncio.run(standardize_batch(model, [(1, "邮费多少")], batch_size=10))
    assert got == [Standardized(1, "邮费多少", None)]


def test_normalized_question_over_512_chars_is_truncated():
    model = fake_reply({"items": [{"pool_id": 1, "normalized_question": "运" * 600, "sample_answer": None}]})
    got = asyncio.run(standardize_batch(model, [(1, "邮费多少")], batch_size=10))
    assert len(got[0].normalized_question) <= 512


def test_find_match_returns_candidate_id():
    model = fake_reply({"matched_id": 3})
    got = asyncio.run(find_match(model, "运费标准", [(3, "运费标准"), (4, "退货流程")]))
    assert got == 3


def test_find_match_rejects_hallucinated_id():
    model = fake_reply({"matched_id": 42})
    assert asyncio.run(find_match(model, "运费标准", [(3, "运费标准")])) is None


def test_find_match_none_means_new_gap():
    model = fake_reply({"matched_id": None})
    assert asyncio.run(find_match(model, "运费标准", [(3, "运费标准")])) is None


def test_find_match_rejects_boolean_matched_id():
    # bool 是 int 的子类：不显式拦住，{"matched_id": true} 会被 int() 成 1，误合并成候选 1。
    model = fake_reply({"matched_id": True})
    assert asyncio.run(find_match(model, "运费标准", [(1, "运费标准")])) is None


def test_find_match_with_no_candidates_skips_the_model():
    model = ScriptedModel([[msg_chunk("不该被调用")]])
    assert asyncio.run(find_match(model, "运费标准", [])) is None
    assert model.calls == 0


def test_standardize_prompt_includes_recall_chunks():
    model = fake_reply({"items": [{"pool_id": 1, "normalized_question": "运费标准",
                                   "sample_answer": "满 99 包邮"}]})
    got = asyncio.run(standardize_batch(model, [(1, "邮费多少")],
                                        contexts={1: [{"question": "运费", "answer": "满 99 包邮"}]}))
    assert got[0].sample_answer == "满 99 包邮"
    assert "满 99 包邮" in model.prompt and "召回片段" in model.prompt


def test_standardize_without_contexts_ignores_the_block():
    model = fake_reply({"items": [{"pool_id": 1, "normalized_question": "运费标准",
                                   "sample_answer": None}]})
    got = asyncio.run(standardize_batch(model, [(1, "邮费多少")]))
    assert got[0].sample_answer is None
    # 只断言「片段块」不在：system prompt 里也有「召回片段」字样（规则 3），负向断言必须钉块头。
    assert "召回片段（写 sample_answer" not in model.prompt
