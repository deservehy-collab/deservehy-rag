import json
from types import SimpleNamespace

import scripts.evaluate_topic_classifier as script

PREDICTIONS = {"买大了想退": ["退换货"], "样本一": ["物流"], "真实原文XYZ": ["运费"]}


class StubModel:
    def __init__(self, *args, **kwargs):
        pass

    def predict(self, texts):
        return [SimpleNamespace(labels=PREDICTIONS.get(text, ["其他"])) for text in texts]


def write(path, rows):
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                    encoding="utf-8")
    return path


def run(tmp_path, monkeypatch, cases):
    monkeypatch.setattr(script, "TopicModel", StubModel)
    test = write(tmp_path / "test.jsonl",
                 [{"id": "t1", "text": "样本一", "labels": ["物流", "运费"]}])
    real = write(tmp_path / "real.jsonl",
                 [{"id": "r1", "text": "真实原文XYZ", "labels": ["运费"]}])
    case_file = write(tmp_path / "cases.jsonl", cases)
    return test, real, case_file, script.main([
        "--test", str(test),
        "--cases", str(case_file),
        "--real", str(real),
        "--out-dir", str(tmp_path / "reports"),
        "--errors", str(tmp_path / "error_review.csv"),
        "--real-out", str(tmp_path / "real_spotcheck.md"),
    ])


def test_missing_hard_case_returns_2(tmp_path, monkeypatch):
    _, _, _, code = run(tmp_path, monkeypatch,
                        [{"text": "运费怎么算", "expected": ["运费"]}])
    assert code == 2


def test_hard_case_failure_returns_3_and_keeps_real_text_out_of_report(tmp_path, monkeypatch):
    errors = tmp_path / "error_review.csv"
    errors.write_text("id,text,true_labels,pred_labels,error_type,note\n"
                      "t1,样本一,\"物流,运费\",物流,模型容量,保留这条结论\n", encoding="utf-8-sig")
    _, _, _, code = run(tmp_path, monkeypatch, [
        {"text": "买大了想退", "expected": ["退换货", "尺码"]},
        {"text": "运费怎么算", "expected": ["运费"]},
    ])
    assert code == 3
    [report] = (tmp_path / "reports").glob("ch10-topic-*.md")
    text = report.read_text(encoding="utf-8")
    assert "共 1 条" in text and "真实原文XYZ" not in text
    assert "模型容量" in text and "保留这条结论" in text
    assert "判错复核待人工" not in text
    assert "真实原文XYZ" in (tmp_path / "real_spotcheck.md").read_text(encoding="utf-8")
    assert "保留这条结论" in errors.read_text(encoding="utf-8-sig")
