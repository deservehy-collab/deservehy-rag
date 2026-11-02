from scripts.report_eval_trend import METRICS, compare, declined, render
OLD = {"strategy": "hybrid_rerank", "recall@5": 0.80, "recall@10": 0.90, "mrr@10": 0.70,
       "refusal_rate": 1.0, "faithfulness": 0.90, "confidence_min": 0.45}


def test_compare_marks_direction():
    current = {**OLD, "recall@5": 0.85, "mrr@10": 0.60}

    deltas = compare(OLD, current)

    assert deltas["recall@5"] == "+0.050 ↑"
    assert deltas["mrr@10"] == "-0.100 ↓"
    assert deltas["recall@10"] == "=0.000 ="
    assert deltas["refusal_rate"] == "=0.000 ="


def test_compare_against_missing_faithfulness_is_na():
    assert compare(OLD, {**OLD, "faithfulness": None})["faithfulness"] == "n/a"
    assert compare({**OLD, "faithfulness": None}, OLD)["faithfulness"] == "n/a"


def test_first_run_has_no_deltas():
    assert compare(None, OLD) == {name: "" for name in METRICS}


def test_declined_only_on_real_drop():
    assert declined(compare(OLD, {**OLD, "recall@5": 0.75})) is True
    assert declined(compare(OLD, {**OLD, "recall@5": 0.85})) is False
    assert declined(compare(OLD, {**OLD, "faithfulness": None})) is False
    assert declined(compare(None, OLD)) is False


def test_render_flags_declining_row_and_keeps_columns():
    rows = [
        {"id": 1, "triggered_by": "手动", "dataset_size": 80, "metrics": OLD,
         "created_at": "2026-10-31 10:00:00"},
        {"id": 2, "triggered_by": "手动", "dataset_size": 80,
         "metrics": {**OLD, "recall@5": 0.75}, "created_at": "2026-10-31 11:00:00"},
    ]

    text = render(rows)

    assert "!! 2 |" in text and "!! 1 |" not in text      # 第一轮没有前一轮，不该被标下滑
    assert "不可答拒答率" in text and "confidence_min" in text
    assert "-0.050 ↓" in text and "0.450" in text


def test_render_empty_is_a_hint_not_a_crash():
    assert "还没有评估记录" in render([])


def test_main_exits_nonzero_when_no_runs(monkeypatch, capsys, tmp_path):
    """空表不能当成功：CI/脚本里退出码为 0 等于宣称「趋势已验收」。"""
    from scripts import report_eval_trend as trend

    class FakeEngine:
        def dispose(self):
            pass

    monkeypatch.setattr(trend.Settings, "from_env", classmethod(lambda cls: object()))
    monkeypatch.setattr(trend, "build_database_url", lambda settings: "sqlite://")
    monkeypatch.setattr(trend, "build_engine", lambda url: FakeEngine())
    monkeypatch.setattr(trend, "build_session_factory", lambda engine: None)
    monkeypatch.setattr(trend, "load_runs", lambda factory, limit: [])
    report_dir = tmp_path / "reports"
    monkeypatch.setattr("sys.argv", ["report_eval_trend.py", "--report-dir", str(report_dir)])

    assert trend.main() == 1
    assert "还没有评估记录" in capsys.readouterr().out
    assert not report_dir.exists()       # 没有记录就不该产出「报告」文件

