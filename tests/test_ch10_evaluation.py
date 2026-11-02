from app.topics.evaluation import compute_metrics, render_html, render_markdown, top_confusions
from app.topics.taxonomy import LABELS


def vec(*indexes):
    row = [0] * len(LABELS)
    for index in indexes:
        row[index] = 1
    return row


def test_perfect_predictions():
    truths = [vec(0, 2), vec(1), vec(16)]
    metrics = compute_metrics(truths, truths)
    assert metrics["macro_f1"] == 1.0 and metrics["micro_f1"] == 1.0
    assert metrics["subset_accuracy"] == 1.0 and metrics["hamming_loss"] == 0.0
    assert metrics["confusion"]["退换货"] == {"tp": 1, "fp": 0, "fn": 0, "tn": 2}


def test_false_negative_and_false_positive_land_in_confusion():
    metrics = compute_metrics([vec(1)], [vec(5)])
    assert metrics["confusion"]["物流"] == {"tp": 0, "fp": 0, "fn": 1, "tn": 0}
    assert metrics["confusion"]["运费"] == {"tp": 0, "fp": 1, "fn": 0, "tn": 0}
    assert metrics["per_label"]["物流"]["recall"] == 0.0
    assert metrics["per_label"]["运费"]["precision"] == 0.0


def test_cooccurrence_counts_truth_by_pred_and_top_confusions_skip_diagonal():
    metrics = compute_metrics([vec(0), vec(4)], [vec(2), vec(4)])
    co = metrics["cooccurrence"]
    assert co[LABELS.index("退换货")][LABELS.index("尺码")] == 1
    assert co[LABELS.index("质量问题")][LABELS.index("质量问题")] == 1
    pairs = top_confusions(co)
    assert pairs[0] == {"true": "退换货", "pred": "尺码", "count": 1}
    assert all(pair["true"] != pair["pred"] for pair in pairs)


def test_renderers_contain_labels_and_no_matplotlib():
    metrics = compute_metrics([vec(0)], [vec(0)])
    markdown = render_markdown(metrics, ["多标签验收：通过"])
    assert "| 类目 | P | R | F1 | support |" in markdown
    assert "多标签验收：通过" in markdown
    html = render_html(metrics)
    assert "退换货" in html and "真:退换货" in html
    assert "matplotlib" not in html
