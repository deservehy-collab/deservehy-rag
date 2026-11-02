import pytest

from app.topics.dataset import (
    Sample, dedupe, iterative_split, make_sample, read_jsonl, write_jsonl,
)


def sample(i, labels, text=None):
    return make_sample(id=f"s{i:03d}", text=text or f"问题{i}", labels=labels)


def test_make_sample_orders_labels_and_rejects_empty_text():
    item = make_sample(id="s1", text="  买大了想退  ", labels=["尺码", "退换货", "尺码"])
    assert item.text == "买大了想退"
    assert item.labels == ["退换货", "尺码"]
    with pytest.raises(ValueError):
        make_sample(id="s2", text="   ", labels=["物流"])
    with pytest.raises(ValueError, match="未知主题标签"):
        make_sample(id="s3", text="退款", labels=["退款"])


def test_jsonl_roundtrip(tmp_path):
    path = tmp_path / "x.jsonl"
    items = [sample(1, ["物流"]), make_sample(id="s2", text="发票怎么开", labels=["发票"],
                                              prelabels=["发票"])]
    write_jsonl(path, items)
    assert read_jsonl(path) == items


def test_dedupe_exact_and_near_duplicate():
    # 字符 3-gram：B 只比 A 多一个尾字，Jaccard = 10/11 > 0.9
    a = "一二三四五六七八九十一二"
    b = a + "三"
    kept, dropped = dedupe([sample(1, ["物流"], "快递到哪了"),
                            sample(2, ["物流"], " 快递到哪了 "),
                            sample(3, ["物流"], a),
                            sample(4, ["物流"], b)])
    assert [item.id for item in kept] == ["s001", "s003"]
    assert dropped == 2


def test_dedupe_keeps_first_occurrence_and_order():
    kept, dropped = dedupe([sample(3, ["物流"], "同一句"), sample(1, ["物流"], "同一句")])
    assert [item.id for item in kept] == ["s003"] and dropped == 1


def _corpus():
    labels = ["退换货", "物流", "尺码", "发票", "运费"]
    items = []
    for i in range(100):
        first = labels[i % len(labels)]
        pair = [first] if i % 4 else [first, labels[(i + 1) % len(labels)]]
        items.append(sample(i, pair, f"第{i}条不同的问题描述"))
    return items


def test_split_is_disjoint_cover_and_reproducible():
    items = _corpus()
    first = iterative_split(items, seed=42)
    second = iterative_split(items, seed=42)
    groups = [first[name] for name in ("train", "val", "test")]
    ids = [[item.id for item in group] for group in groups]
    assert sum(len(group) for group in groups) == len(items)
    assert len(set().union(*[set(group) for group in ids])) == len(items)
    assert ids == [[item.id for item in second[name]] for name in ("train", "val", "test")]
    assert abs(len(first["val"]) / len(items) - 0.1) <= 0.02
    assert abs(len(first["test"]) / len(items) - 0.1) <= 0.02


def test_split_covers_frequent_label_in_val_and_test():
    items = _corpus()
    splits = iterative_split(items, seed=42)
    for name in ("val", "test"):
        present = {label for item in splits[name] for label in item.labels}
        assert {"退换货", "物流", "尺码", "发票", "运费"} <= present


def test_split_is_balanced_per_label():
    splits = iterative_split(_corpus(), seed=42)
    for label in ("退换货", "物流", "尺码", "发票", "运费"):
        count = sum(1 for item in splits["val"] if label in item.labels)
        assert 1 <= count <= 4


from app.topics.dataset import (
    SYNONYMS, apply_corrections, apply_synonyms, assemble_splits, attach_paraphrases,
)


def test_synonyms_have_required_pairs_and_no_duplicate_source():
    pairs = dict(SYNONYMS)
    assert pairs["快递"] == "物流"
    assert pairs["包邮"] == "免运费"
    assert pairs["开票"] == "开发票"
    assert pairs["价保"] == "保价"
    assert pairs["补货"] == "到货"
    assert pairs["积分"] == "会员分"
    sources = [source for source, _ in SYNONYMS]
    assert len(sources) == len(set(sources))


def test_apply_synonyms_at_most_two_and_skips_existing_target():
    import random

    text = "快递包邮吗，开票怎么弄"
    replaced = apply_synonyms(text, random.Random(1), max_replacements=2)
    assert sum(word in replaced for word in ("物流", "免运费", "开发票")) == 2
    # 原文已有「物流」时不再把「快递」替换成第二个「物流」
    assert apply_synonyms("物流快递", random.Random(1)) == "物流快递"


def test_attach_paraphrases_only_when_seed_in_train():
    train = [sample(1, ["退换货"]), sample(2, ["物流"]), sample(3, ["发票"])]
    paras = [
        make_sample(id="p1", text="退换货改写", labels=["退换货"], origin="paraphrase",
                    seed_id="s001"),
        make_sample(id="p2", text="物流改写", labels=["物流"], origin="paraphrase",
                    seed_id="s999"),
    ]
    merged = attach_paraphrases(train, paras, ratio=1.0, seed=42)
    ids = [item.id for item in merged]
    assert "p1" in ids and "p2" not in ids


def test_apply_corrections_overrides_labels_and_rejects_unknown():
    items = [sample(1, ["物流"]), sample(2, ["发票"])]
    fixed = apply_corrections(items, {"s001": ["运费", "物流"]})
    assert fixed[0].labels == ["物流", "运费"]
    assert fixed[1].labels == ["发票"]
    with pytest.raises(ValueError, match="未知主题标签"):
        apply_corrections(items, {"s001": ["退款"]})


def test_assemble_splits_appends_real_only_to_train_and_augments_train_only():
    synthetic = _corpus()
    real = [make_sample(id="real-1", text="真实问题", labels=["物流"], origin="real")]
    # 每个合成样本都配一条改写；切分后 train 里至少有一个 seed 能挂上改写
    paras = [make_sample(id=f"p{i:03d}", text=f"第{i}条不同的问题描述改写",
                         labels=synthetic[i].labels, origin="paraphrase",
                         seed_id=f"s{i:03d}") for i in range(100)]
    splits = assemble_splits(synthetic, real=real, paraphrases=paras, seed=42)
    train_ids = {item.id for item in splits["train"]}
    assert "real-1" in train_ids
    assert all(item.id != "real-1" for name in ("val", "test") for item in splits[name])
    assert any(item.origin == "paraphrase" for item in splits["train"])
    # 验证/测试集绝不出现改写样本
    assert all(item.origin != "paraphrase" for name in ("val", "test") for item in splits[name])


def test_assemble_dedupes_synthetic_before_split():
    synthetic = _corpus()
    duplicate = sample(999, synthetic[0].labels, synthetic[0].text)
    splits = assemble_splits(synthetic + [duplicate], seed=42)
    ids = [item.id for group in splits.values() for item in group
           if item.origin == "synthetic"]
    assert len(ids) == len(set(ids)) == 100


def test_assemble_drops_real_duplicate_of_val_or_test():
    synthetic = _corpus()
    baseline = assemble_splits(synthetic, seed=42)
    twin = baseline["val"][0]
    real = [make_sample(id="real-dup", text=twin.text, labels=twin.labels, origin="real")]
    splits = assemble_splits(synthetic, real=real, seed=42)
    assert all(item.id != "real-dup" for group in splits.values() for item in group)
