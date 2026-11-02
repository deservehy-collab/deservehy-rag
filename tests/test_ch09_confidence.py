"""ch09 证据置信度契约：三信号取值、钳位、非精排不拦、召回快照形状。"""

import dataclasses

import pytest

from app.knowledge.confidence import EvidenceConfidence, score, snapshot_of
from app.knowledge.retrieval import Evidence


def item(rerank, **scores):
    return Evidence(id=1, question="q", answer="a" * 600, category="c",
                    section_path="s", content_type="policy",
                    scores={"rerank": rerank, **scores})


def test_three_signals_are_reported():
    conf = score([item(0.80), item(0.55), item(0.30), item(0.10)], effective_floor=0.20)
    assert conf.signals == {"top1": 0.80, "effective_count": 3, "margin": 0.25,
                            "norm_count": 1.0}
    assert conf.value == pytest.approx(0.7 * 0.80 + 0.2 * 1.0 + 0.1 * 0.25)


def test_effective_count_capped_at_three():
    five = score([item(0.9), item(0.4), item(0.3), item(0.25), item(0.2)], effective_floor=0.2)
    three = score([item(0.9), item(0.4), item(0.3)], effective_floor=0.2)
    assert five.signals["effective_count"] == 5
    assert five.value == pytest.approx(three.value)


def test_effective_count_respects_floor():
    # effective_floor 是「够格算证据」的门槛，低于它的命中不计入 effective_count（规格 §6.1）。
    low = score([item(0.9), item(0.5), item(0.15)], effective_floor=0.2)
    assert low.signals["effective_count"] == 2
    assert low.signals["norm_count"] == pytest.approx(2 / 3, rel=1e-3)
    high = score([item(0.9), item(0.5), item(0.15)], effective_floor=0.1)
    assert high.signals["effective_count"] == 3


def test_single_hit_margin_is_top1():
    # 只有一条命中：没有竞争者，margin 记为 top1 本身（规格 §6.1）。
    conf = score([item(0.6)], effective_floor=0.2)
    assert conf.signals["margin"] == pytest.approx(0.6)
    assert conf.signals["effective_count"] == 1


def test_empty_items_is_zero():
    conf = score([], effective_floor=0.2)
    assert conf.value == 0.0
    assert conf.signals == {"top1": 0.0, "effective_count": 0, "margin": 0.0, "norm_count": 0.0}
    assert conf.reason
    # 生成器也算空：不许被列表推导耗尽后又被 `if items:` 误判成「有召回但无精排分」。
    empty_gen = score(iter([]), effective_floor=0.2)
    assert empty_gen.value == 0.0
    assert empty_gen.reason == "no_evidence"


def test_no_rerank_strategy_is_not_blocked():
    # dense/bm25/hybrid 没有 rerank 分 → 等价于今天「不拦」的行为。
    conf = score([Evidence(id=1, question="q", answer="a", category="c",
                           section_path="s", content_type="policy", scores={"rrf": 0.01})])
    assert conf.value == 1.0
    assert conf.reason == "no_rerank"


def test_value_is_clamped_to_unit_interval():
    weird = score([item(1.0), item(1.0), item(1.0)], w_top1=0.9, w_count=0.9, w_margin=0.9)
    assert weird.value == pytest.approx(1.0)


def test_weights_are_actually_applied():
    # 只留 top1 权重时必须是 0.5；默认权重按 0.7/0.2/0.1 三项加权（写死权重的实现过不了第一条）。
    assert score([item(0.5)], w_top1=1.0, w_count=0.0, w_margin=0.0).value == pytest.approx(0.5)
    assert score([item(0.5)]).value == pytest.approx(0.7 * 0.5 + 0.2 * (1 / 3) + 0.1 * 0.5)


def test_all_hits_below_floor_still_scores_from_top1_and_margin():
    conf = score([item(0.10), item(0.05)], effective_floor=0.20)
    assert conf.signals["effective_count"] == 0
    assert conf.signals["norm_count"] == 0.0
    assert conf.value == pytest.approx(0.7 * 0.10 + 0.1 * (0.10 - 0.05))


def test_weights_are_defaults_not_required_args():
    assert score([item(0.5)]).value == pytest.approx(0.7 * 0.5 + 0.2 * (1 / 3) + 0.1 * 0.5)


def test_snapshot_shape_and_truncation():
    snap = snapshot_of([item(0.8, rrf=0.03), item(0.4)], limit=5, answer_chars=500)
    assert [row["n"] for row in snap] == [1, 2]
    assert snap[0]["chunk_id"] == 1
    assert len(snap[0]["answer"]) == 500
    assert snap[0]["scores"] == {"rerank": 0.8, "rrf": 0.03}
    assert "section_path" in snap[0] and "question" in snap[0]


def test_snapshot_respects_limit_and_answer_chars():
    items = [item(0.8), item(0.4), item(0.3)]
    assert len(snapshot_of(items, limit=1)) == 1
    assert len(snapshot_of(items, limit=2)) == 2
    assert len(snapshot_of(items, limit=2)[1]["answer"]) == 500   # 默认截断
    short = snapshot_of(items, limit=1, answer_chars=10)[0]["answer"]
    assert len(short) == 10
    # 负 limit 不许变成「反向丢最后一条」：取下界 0。
    assert snapshot_of(items, limit=-1) == []


def test_snapshot_of_empty_is_empty():
    assert snapshot_of([]) == []
    assert snapshot_of(iter([])) == []


def test_dataclass_is_frozen():
    conf = EvidenceConfidence(value=1.0, signals={}, reason="")
    with pytest.raises(dataclasses.FrozenInstanceError):
        conf.value = 0.5
