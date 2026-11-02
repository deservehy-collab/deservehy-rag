"""多标签评测指标与 Markdown/HTML 报告；HTML 热力图不依赖 matplotlib。"""

from __future__ import annotations

import html as html_lib

import numpy as np
from sklearn.metrics import f1_score, hamming_loss, precision_recall_fscore_support

from app.topics.taxonomy import LABELS


def compute_metrics(truths, preds) -> dict:
    true = np.asarray(truths, dtype=int)
    pred = np.asarray(preds, dtype=int)
    precision, recall, f1, support = precision_recall_fscore_support(
        true, pred, labels=list(range(len(LABELS))), average=None, zero_division=0)
    per_label, confusion = {}, {}
    for index, label in enumerate(LABELS):
        tp = int(((true[:, index] == 1) & (pred[:, index] == 1)).sum())
        fp = int(((true[:, index] == 0) & (pred[:, index] == 1)).sum())
        fn = int(((true[:, index] == 1) & (pred[:, index] == 0)).sum())
        tn = int(((true[:, index] == 0) & (pred[:, index] == 0)).sum())
        per_label[label] = {"precision": round(float(precision[index]), 4),
                            "recall": round(float(recall[index]), 4),
                            "f1": round(float(f1[index]), 4), "support": int(support[index])}
        confusion[label] = {"tp": tp, "fp": fp, "fn": fn, "tn": tn}
    cooccurrence = np.zeros((len(LABELS), len(LABELS)), dtype=int)
    for truth_row, pred_row in zip(true, pred):
        for true_index in np.where(truth_row == 1)[0]:
            for pred_index in np.where(pred_row == 1)[0]:
                cooccurrence[true_index, pred_index] += 1
    # multi-label 指示矩阵下 sklearn 默认按 17 列全部参与 macro，缺样本的类目会被算成 0 分；
    # 只对「真或预测出现过」的类目求平均，才和 scripts/train_topic_classifier.py 的 macro_f1
    # 同口径（train/val/test 三份切分 17 类均有样本，因此真实评测与 17 类全平均等价）。
    average_labels = [index for index in range(len(LABELS))
                      if true[:, index].any() or pred[:, index].any()]
    return {
        "per_label": per_label,
        "macro_f1": round(float(f1_score(true, pred, labels=average_labels,
                                         average="macro", zero_division=0)), 4),
        "micro_f1": round(float(f1_score(true, pred, average="micro", zero_division=0)), 4),
        "subset_accuracy": round(float((true == pred).all(axis=1).mean()), 4),
        "hamming_loss": round(float(hamming_loss(true, pred)), 4),
        "confusion": confusion,
        "cooccurrence": cooccurrence.tolist(),
    }


def top_confusions(cooccurrence, top: int = 15) -> list[dict]:
    pairs = []
    for true_index, true_label in enumerate(LABELS):
        for pred_index, pred_label in enumerate(LABELS):
            count = int(cooccurrence[true_index][pred_index])
            if true_index != pred_index and count:
                pairs.append({"true": true_label, "pred": pred_label, "count": count})
    return sorted(pairs, key=lambda row: (-row["count"], row["true"], row["pred"]))[:top]


def render_markdown(metrics: dict, extra_lines=()) -> str:
    lines = ["# ch10 主题分类评测报告", "",
             f"- macro-F1：{metrics['macro_f1']}", f"- micro-F1：{metrics['micro_f1']}",
             f"- 整条完全命中：{metrics['subset_accuracy']}",
             f"- Hamming loss：{metrics['hamming_loss']}", "",
             "| 类目 | P | R | F1 | support | TP | FP | FN | TN |",
             "|---|---|---|---|---|---|---|---|---|"]
    for label in LABELS:
        row, matrix = metrics["per_label"][label], metrics["confusion"][label]
        lines.append(f"| {label} | {row['precision']} | {row['recall']} | {row['f1']} | "
                     f"{row['support']} | {matrix['tp']} | {matrix['fp']} | "
                     f"{matrix['fn']} | {matrix['tn']} |")
    lines += ["", "## Top 混淆对", "", "| 真标签 | 预测标签 | 次数 |", "|---|---|---|"]
    for pair in top_confusions(metrics["cooccurrence"]):
        lines.append(f"| {pair['true']} | {pair['pred']} | {pair['count']} |")
    lines += ["", *extra_lines]
    return "\n".join(lines) + "\n"


def render_html(metrics: dict) -> str:
    cooccurrence = metrics["cooccurrence"]
    peak = max((max(row) for row in cooccurrence), default=0) or 1
    header = "".join(f"<th>{html_lib.escape(label)}</th>" for label in LABELS)
    rows = []
    for true_index, true_label in enumerate(LABELS):
        cells = [f"<th>{html_lib.escape(true_label)}</th>"]
        for pred_index, pred_label in enumerate(LABELS):
            value = cooccurrence[true_index][pred_index]
            alpha = value / peak
            cells.append(
                f"<td title='真:{html_lib.escape(true_label)} 预测:"
                f"{html_lib.escape(pred_label)} 次数:{value}' "
                f"style='background:rgba(46,125,110,{alpha:.3f})'>{value or ''}</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<title>ch10 混淆矩阵</title><style>"
        "body{font-family:system-ui,sans-serif;margin:24px;color:#243831}"
        "table{border-collapse:collapse;font-size:12px}"
        "th,td{border:1px solid #dce5e2;padding:4px 6px;text-align:center}"
        "th{background:#f2f7f5}td{min-width:28px}"
        "</style></head><body><h1>17×17 真标签 × 预测标签</h1>"
        f"<table><thead><tr><th>真 \\ 预测</th>{header}</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></body></html>"
    )
