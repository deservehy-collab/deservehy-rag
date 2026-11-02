import csv
import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.topics.dataset import make_sample, read_jsonl
from app.topics.prompts import parse_payload
from app.topics.taxonomy import LABELS
from scripts.build_topic_dataset import (
    COMBOS, QUOTAS, TOTAL, export_audit, generate_group, plan_generation, run_paraphrase, run_split,
)


class FakeLLM:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.prompts = []

    def invoke(self, prompt):
        self.prompts.append(prompt)
        return SimpleNamespace(content=self.outputs.pop(0))


def test_generate_prompt_demands_both_intents_for_multilabel():
    from app.topics.prompts import generate_prompt

    prompt = generate_prompt(["物流", "运费"], 5)
    assert "多标签批次" in prompt
    assert "缺一个就算不合格" in prompt
    assert "只写了物流" in prompt
    single = generate_prompt(["物流"], 5)
    assert "多标签批次" not in single


def test_plan_generation_hits_total_and_quotas():
    plan = plan_generation()
    assert sum(item["count"] for item in plan) == TOTAL == 1200
    assert len(COMBOS) == 10
    assert sum(item["count"] for item in plan if len(item["labels"]) == 2) == 300
    support = Counter()
    for item in plan:
        for label in item["labels"]:
            support[label] += item["count"]
    assert set(support) == set(LABELS)
    assert all(support[label] >= QUOTAS[label] for label in LABELS)


def test_parse_payload_accepts_fenced_json_and_rejects_garbage():
    assert parse_payload('```json\n{"items":[{"question":"在吗"}]}\n```')["items"][0]["question"] == "在吗"
    with pytest.raises(ValueError):
        parse_payload("没有 JSON")
    with pytest.raises(ValueError):
        parse_payload("[1, 2]")


def test_generate_group_retries_once_then_succeeds():
    llm = FakeLLM(["不是 JSON", json.dumps({"items": [{"question": "快递到哪了"},
                                                       {"question": "什么时候发货"}]},
                                          ensure_ascii=False)])
    assert generate_group(llm, ["物流"], 2) == ["快递到哪了", "什么时候发货"]
    assert len(llm.prompts) == 2


def test_generate_group_raises_instead_of_silently_short():
    llm = FakeLLM(["坏输出", "还是坏输出"])
    with pytest.raises(RuntimeError, match="生成失败"):
        generate_group(llm, ["物流"], 2)


def test_run_generate_replaces_duplicate_questions(monkeypatch, tmp_path):
    import scripts.build_topic_dataset as build

    monkeypatch.setattr(build, "plan_generation", lambda: [{"labels": ["物流"], "count": 3}])
    monkeypatch.setattr(build, "TOTAL", 3)
    llm = FakeLLM([
        json.dumps({"items": [{"question": "快递到哪了"}, {"question": "快递到哪了"},
                              {"question": "什么时候发货"}, {"question": "几天能到"}]},
                   ensure_ascii=False),
        json.dumps({"items": [{"question": "包裹在路上了吗"}]}, ensure_ascii=False),
    ])
    samples = build.run_generate(llm, tmp_path / "s.jsonl", tmp_path)
    assert [item.id for item in samples] == ["syn-0001", "syn-0002", "syn-0003"]
    assert len({item.text for item in samples}) == 3


def test_run_generate_rejects_near_duplicates_across_groups(monkeypatch, tmp_path):
    import scripts.build_topic_dataset as build
    from app.topics.dataset import jaccard

    monkeypatch.setattr(build, "plan_generation", lambda: [
        {"labels": ["物流"], "count": 2}, {"labels": ["运费"], "count": 2}])
    monkeypatch.setattr(build, "TOTAL", 4)
    shared = "我的快递今天到底什么时候能送到啊我等了很久"
    llm = FakeLLM([
        json.dumps({"items": [{"question": shared},
                              {"question": "包裹显示已发货但一直没有物流更新信息"},
                              {"question": "快递员直接把包裹放门口没有打电话通知"},
                              {"question": "物流状态停在派送中已经三天没有变化了"}]},
                   ensure_ascii=False),
        # 前两条分别是第一条的近重复与精确重复，跨组也必须收集阶段就丢掉。
        json.dumps({"items": [{"question": shared + "呀"},
                              {"question": shared},
                              {"question": "这个运费为什么比商品页面上写的贵了五块钱"},
                              {"question": "满多少金额才可以免运费请说明一下规则"}]},
                   ensure_ascii=False),
    ])
    samples = build.run_generate(llm, tmp_path / "s.jsonl", tmp_path)
    texts = [item.text for item in samples]
    assert len(samples) == 4
    assert shared + "呀" not in texts
    assert all(jaccard(left, right) < 0.9
               for index, left in enumerate(texts) for right in texts[index + 1:])


def test_export_audit_is_deterministic_and_per_class(tmp_path):
    samples = [make_sample(id=f"s{i:03d}", text=f"第{i}条问题", labels=[LABELS[i % 17]],
                           prelabels=[LABELS[i % 17]]) for i in range(17 * 6)]
    left, right = tmp_path / "a.csv", tmp_path / "b.csv"
    assert export_audit(samples, left, per_class=2) == 34
    export_audit(samples, right, per_class=2)
    assert left.read_text(encoding="utf-8-sig") == right.read_text(encoding="utf-8-sig")
    rows = list(csv.DictReader(left.open(encoding="utf-8-sig")))
    assert len(rows) == 34
    assert all(row["correct_labels"] == "" and row["note"] == "" for row in rows)


def test_run_paraphrase_keeps_only_label_preserving_rewrites(tmp_path):
    seeds = tmp_path / "seeds.jsonl"
    from app.topics.dataset import write_jsonl

    write_jsonl(seeds, [make_sample(id="s1", text="快递到哪了", labels=["物流"]),
                        make_sample(id="s2", text="发票怎么开", labels=["发票"])])
    llm = FakeLLM([
        json.dumps({"items": [{"id": "s1", "text": "我的包裹现在到哪了"},
                              {"id": "s2", "text": "怎么开发票"}]}, ensure_ascii=False),
        json.dumps({"items": [{"id": "par-0001", "labels": ["物流"]},
                              {"id": "par-0002", "labels": ["支付"]}]}, ensure_ascii=False),
    ])
    out = tmp_path / "par.jsonl"
    kept, dropped = run_paraphrase(llm, seeds, out)
    assert (kept, dropped) == (1, 1)
    items = read_jsonl(out)
    assert items[0].seed_id == "s1" and items[0].labels == ["物流"]


def test_run_split_appends_real_and_keeps_paraphrase_out_of_val_test(tmp_path):
    from app.topics.dataset import write_jsonl

    labels = ["退换货", "物流", "尺码", "发票", "运费"]
    synthetic = [make_sample(id=f"s{i:03d}", text=f"第{i}条不同问法", labels=[labels[i % 5]])
                 for i in range(100)]
    synthetic_path = tmp_path / "syn.jsonl"
    write_jsonl(synthetic_path, synthetic)
    real_path = tmp_path / "real.jsonl"
    write_jsonl(real_path, [make_sample(id="real-1", text="真实问题", labels=["物流"], origin="real")])
    para_path = tmp_path / "par.jsonl"
    write_jsonl(para_path, [make_sample(id=f"p{i:03d}", text=f"改写第{i}条不同问法",
                                        labels=[labels[i % 5]], origin="paraphrase",
                                        seed_id=f"s{i:03d}") for i in range(100)])

    splits = run_split(synthetic_path, tmp_path / "splits", real_path=real_path,
                       paraphrase_path=para_path, seed=42, report_dir=tmp_path)
    train_ids = {item.id for item in splits["train"]}
    assert "real-1" in train_ids
    assert any(item.origin == "paraphrase" for item in splits["train"])
    for name in ("val", "test"):
        assert all(item.origin != "paraphrase" for item in splits[name])
    assert (tmp_path / "splits" / "train.jsonl").exists()


def test_run_prelabel_handles_empty_file(tmp_path, capsys):
    from app.topics.dataset import write_jsonl
    from scripts.build_topic_dataset import run_prelabel

    path = tmp_path / "empty.jsonl"
    write_jsonl(path, [])
    assert run_prelabel(FakeLLM([]), path) == []
    assert "为空" in capsys.readouterr().out


def test_run_prelabel_total_failure_preserves_existing_file(tmp_path, capsys):
    from app.topics.dataset import make_sample, write_jsonl
    from scripts.build_topic_dataset import run_prelabel

    class BoomLLM:
        def invoke(self, prompt):
            raise RuntimeError("upstream down")

    path = tmp_path / "samples.jsonl"
    write_jsonl(path, [make_sample(id="s1", text="怎么查物流", labels=["物流"],
                                   prelabels=["运费"])])
    before = path.read_bytes()
    result = run_prelabel(BoomLLM(), path, batch=1)
    assert path.read_bytes() == before
    assert [item.prelabels for item in result] == [["运费"]]
    assert "保留原文件" in capsys.readouterr().err


def test_parser_allows_paraphrase_mode_and_path_together():
    from scripts.build_topic_dataset import build_parser

    args = build_parser().parse_args(
        ["--paraphrase", "--in", "x.jsonl", "--out", "y.jsonl", "--paraphrase-path", "p.jsonl"])
    assert args.paraphrase is True
    assert args.paraphrase_path == Path("p.jsonl")
