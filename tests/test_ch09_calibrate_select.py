"""ch09 置信度校准的选择规则：只测纯函数，不碰检索/模型。"""

import json

from scripts.calibrate_confidence import (dataset_digest, grid_rows, load_cache, select_cell)


def cell(floor, threshold, refusal, false_refusal):
    return {"effective_floor": floor, "confidence_min": threshold,
            "unanswerable_refusal_rate": refusal, "answerable_false_refusal_rate": false_refusal}


def test_requires_perfect_refusal_on_unanswerable():
    grid = [cell(0.2, 0.30, 0.9, 0.0), cell(0.2, 0.25, 1.0, 0.1), cell(0.2, 0.20, 1.0, 0.05)]
    assert select_cell(grid) == grid[2]


def test_tie_break_prefers_larger_threshold():
    grid = [cell(0.1, 0.20, 1.0, 0.05), cell(0.2, 0.30, 1.0, 0.05)]
    assert select_cell(grid) == grid[1]


def test_floor_varies_independently():
    grid = [cell(0.10, 0.30, 1.0, 0.08), cell(0.25, 0.30, 1.0, 0.04)]
    assert select_cell(grid) == grid[1]


def test_no_cell_qualifies_returns_none():
    assert select_cell([cell(0.2, 0.3, 0.75, 0.0)]) is None


def test_empty_grid_returns_none():
    assert select_cell([]) is None


def test_selected_scores_zeroes_refused_cases():
    from scripts.calibrate_confidence import selected_scores

    per_case = [
        {"answerable": True, "reranks": [0.9, 0.8], "hit_sections": ["A"], "expected_sections": ["A"]},
        {"answerable": True, "reranks": [0.1], "hit_sections": ["B"], "expected_sections": ["B"]},
    ]
    # 第 1 题 value≈0.773（0.7*0.9+0.2*2/3+0.1*0.1）不拦且命中；第 2 题 value≈0.08 被拦 ⇒ 指标按 0 计
    assert selected_scores(per_case, floor=0.2, threshold=0.5) == {"recall@10": 0.5, "mrr@10": 0.5}


def test_floor_tie_keeps_grid_order():
    # 同阈值同误拒时没有 floor 的二级键，min() 稳定取网格里靠前的（floor 升序 ⇒ 0.15）。
    grid = [cell(0.15, 0.10, 1.0, 0.0278), cell(0.20, 0.10, 1.0, 0.0278)]
    assert select_cell(grid)["effective_floor"] == 0.15


def test_grid_rows_and_blocked_use_floor_for_effective_count():
    # [0.5, 0.16]：floor=0.15 算两条有效证据 ⇒ value≈0.517 不拦；floor=0.20 只算一条 ⇒ value≈0.451 被 0.50 拦。
    # 把 floor 强制置 0 时两条都会拦，这条用例就是靠这个差异锁住 floor 语义。
    from scripts.calibrate_confidence import blocked

    assert blocked([0.5, 0.16], floor=0.15, threshold=0.50) is False
    assert blocked([0.5, 0.16], floor=0.20, threshold=0.50) is True

    per_case = [
        {"answerable": True, "reranks": [0.5, 0.16]},
        {"answerable": False, "reranks": [0.11]},
    ]
    grid = grid_rows(per_case)

    def cell_at(floor, threshold):
        return next(item for item in grid
                    if item["effective_floor"] == floor and item["confidence_min"] == threshold)

    assert cell_at(0.15, 0.50)["false_refusal_count"] == 0
    assert cell_at(0.20, 0.50)["false_refusal_count"] == 1
    assert cell_at(0.15, 0.50)["unanswerable_refusal_rate"] == 1.0


def test_load_cache_accepts_matching_dataset(tmp_path):
    cases = [{"id": "A01"}]
    payload = {"meta": {"dataset_digest": dataset_digest(cases)},
               "per_case": [{"id": "A01", "reranks": [0.9]}]}
    path = tmp_path / "cache.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert load_cache(path, cases) == payload


def test_load_cache_rejects_stale_dataset(tmp_path):
    path = tmp_path / "cache.json"
    path.write_text(json.dumps({"meta": {"dataset_digest": "deadbeef"},
                                "per_case": [{"id": "A01"}]}), encoding="utf-8")

    assert load_cache(path, [{"id": "A01"}]) is None


def test_load_cache_rejects_legacy_cache_with_wrong_ids(tmp_path):
    path = tmp_path / "cache.json"
    path.write_text(json.dumps([{"id": "A02"}]), encoding="utf-8")

    assert load_cache(path, [{"id": "A01"}]) is None


def test_load_cache_rejects_changed_retrieval_params(tmp_path):
    cases = [{"id": "A01"}]
    payload = {"meta": {"dataset_digest": dataset_digest(cases), "knowledge_top_k": 10},
               "per_case": [{"id": "A01", "reranks": [0.9]}]}
    path = tmp_path / "cache.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert load_cache(path, cases, expected={"knowledge_top_k": 5}) is None
    assert load_cache(path, cases, expected={"knowledge_top_k": 10}) == payload


def test_load_cache_rejects_malformed_file(tmp_path):
    path = tmp_path / "cache.json"
    path.write_text("{not json", encoding="utf-8")
    assert load_cache(path, [{"id": "A01"}]) is None

    path.write_text(json.dumps({"meta": {}}), encoding="utf-8")
    assert load_cache(path, [{"id": "A01"}]) is None
