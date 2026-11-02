"""测试集评测、多标签验收、真实抽检与判错导出。"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path

from app.topics.dataset import read_jsonl
from app.topics.evaluation import compute_metrics, render_html, render_markdown
from app.topics.model import ModelNotReady, TopicModel
from app.topics.taxonomy import LABELS

HARD_CASE = "买大了想退"


def load_cases(path: Path) -> list[dict]:
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
             if line.strip()]
    if not any(case.get("text") == HARD_CASE for case in cases):
        raise ValueError(f"多标签用例缺少硬验收条目：{HARD_CASE}")
    return cases


def load_review(path: Path) -> dict[str, tuple[str, str]]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return {row["id"]: (row.get("error_type", ""), row.get("note", ""))
                for row in csv.DictReader(handle) if row.get("id")}


def matrix(samples) -> list[list[int]]:
    return [[1 if label in item.labels else 0 for label in LABELS] for item in samples]


def label_matrix(labels_list) -> list[list[int]]:
    return [[1 if label in labels else 0 for label in LABELS] for labels in labels_list]


def predict(model, texts, batch=64):
    result = []
    for start in range(0, len(texts), batch):
        result.extend(model.predict(texts[start:start + batch]))
    return result


def pick_errors(samples, predictions, limit=25):
    errors = []
    for item, prediction in zip(samples, predictions):
        false_negative = [label for label in item.labels if label not in prediction.labels]
        false_positive = [label for label in prediction.labels if label not in item.labels]
        if false_negative or false_positive:
            errors.append((item, prediction, false_negative, false_positive))
    errors.sort(key=lambda row: (-(len(row[2]) + len(row[3])), row[0].id))
    chosen, covered = [], set()
    for error in errors:
        classes = set(error[0].labels) | set(error[1].labels)
        if classes - covered:
            chosen.append(error)
            covered |= classes
            if len(chosen) >= limit:
                return chosen
    for error in errors:
        if len(chosen) >= limit:
            break
        if error not in chosen:
            chosen.append(error)
    return chosen


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="ch10 评测")
    parser.add_argument("--test", type=Path, default=Path(".cache/topics/splits/test.jsonl"))
    parser.add_argument("--model-dir", type=Path, default=Path(".cache/models/topic-classifier"))
    parser.add_argument("--cases", type=Path, default=Path("evals/topics/multilabel_cases.jsonl"))
    parser.add_argument("--real", type=Path, default=Path("data/topics/real_train.jsonl"))
    parser.add_argument("--out-dir", type=Path, default=Path("evals/reports"))
    parser.add_argument("--errors", type=Path, default=Path("evals/topics/error_review.csv"))
    parser.add_argument("--real-out", type=Path, default=Path("data/topics/real_spotcheck.md"))
    parser.add_argument("--device", default="cpu")
    args = parser.parse_args(argv)
    if not args.test.exists():
        print(f"测试集不存在：{args.test}，先跑 --split。")
        return 2
    try:
        model = TopicModel(args.model_dir, device=args.device)
    except ModelNotReady as exc:
        print(f"模型未就绪：{exc}，先跑 scripts/train_topic_classifier.py。")
        return 2
    test = read_jsonl(args.test)
    if not test:
        print("测试集为空。")
        return 2
    predictions = predict(model, [item.text for item in test])
    metrics = compute_metrics(matrix(test), label_matrix([p.labels for p in predictions]))

    extra = []
    failed_cases = []
    if not args.cases.exists():
        print(f"多标签用例不存在：{args.cases}，验收③无法判定。")
        return 2
    try:
        cases = load_cases(args.cases)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"多标签用例无法使用：{exc}")
        return 2
    case_predictions = predict(model, [case["text"] for case in cases])
    extra += ["## 多标签验收", "", "| 文本 | 期望 | 预测 | 全部命中 |", "|---|---|---|---|"]
    for case, prediction in zip(cases, case_predictions):
        hit = set(case["expected"]) <= set(prediction.labels)
        extra.append(f"| {case['text']} | {'、'.join(case['expected'])} | "
                     f"{'、'.join(prediction.labels)} | {'是' if hit else '否'} |")
        if not hit:
            failed_cases.append(case["text"])
    if HARD_CASE in failed_cases:
        extra.append(f"\n> 验收③失败：`{HARD_CASE}` 没有同时命中退换货与尺码。")

    real_lines = []
    if args.real.exists():
        real = read_jsonl(args.real)
        real_predictions = predict(model, [item.text for item in real])
        matched = sum(set(item.labels) == set(prediction.labels)
                      for item, prediction in zip(real, real_predictions))
        real_lines = [f"| {item.text} | {'、'.join(item.labels)} | {'、'.join(prediction.labels)} |"
                      for item, prediction in zip(real, real_predictions)]
        extra += ["", "## 真实问题抽检（已进训练集，仅观察真实分布）", "",
                  f"共 {len(real)} 条，预测与人工标签一致 {matched} 条；逐条原文只写入 "
                  f"git-ignored 的 `{args.real_out}`，不进入本报告。"]
    else:
        extra.append("> 未找到真实训练集，跳过真实抽检。")

    errors = pick_errors(test, predictions)
    existing_review = load_review(args.errors)
    review_rows = [(item, prediction, existing_review.get(item.id, ("", "")), false_negative,
                    false_positive)
                   for item, prediction, false_negative, false_positive in errors]
    pending = [row for row in review_rows if not row[2][0]]
    extra += ["", "## 判错复核结论", ""]
    if pending:
        extra.append(f"> 判错复核待人工：{len(pending)}/{len(review_rows)} 条未填 error_type，"
                     "本报告暂不含这些条目的结论。")
    else:
        extra.append(f"{len(review_rows)} 条判错样本已复核"
                     f"（来源：`{args.errors}` 的 error_type/note）。")
    extra += ["", "| id | 文本 | 真标签 | 预测标签 | error_type | note |",
              "|---|---|---|---|---|---|"]
    for item, prediction, (error_type, note), _, _ in review_rows:
        extra.append(f"| {item.id} | {item.text} | {'、'.join(item.labels)} | "
                     f"{'、'.join(prediction.labels)} | {error_type or '待人工'} | {note} |")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / f"ch10-topic-{stamp}.json").write_text(
        json.dumps({"metrics": metrics, "failed_cases": failed_cases},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    (args.out_dir / f"ch10-topic-{stamp}.md").write_text(
        render_markdown(metrics, extra), encoding="utf-8")
    (args.out_dir / f"ch10-topic-{stamp}.html").write_text(
        render_html(metrics), encoding="utf-8")

    if real_lines:
        args.real_out.parent.mkdir(parents=True, exist_ok=True)
        args.real_out.write_text("\n".join([
            "# 真实问题抽检（git-ignored）", "",
            "| 文本 | 人工标签 | 预测标签 |", "|---|---|---|", *real_lines,
        ]) + "\n", encoding="utf-8")
    args.errors.parent.mkdir(parents=True, exist_ok=True)
    with args.errors.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["id", "text", "true_labels", "pred_labels", "error_type", "note"])
        for item, prediction, (error_type, note), _, _ in review_rows:
            writer.writerow([item.id, item.text, ",".join(item.labels),
                             ",".join(prediction.labels), error_type, note])
    print(f"macro-F1={metrics['macro_f1']} micro-F1={metrics['micro_f1']} "
          f"subset={metrics['subset_accuracy']}")
    print(f"报告：{args.out_dir}/ch10-topic-{stamp}.{{json,md,html}}；判错样本：{args.errors}")
    others = [text for text in failed_cases if text != HARD_CASE]
    if others:
        print("以下多标签用例未全部命中（仅提示，不判失败）：" + "、".join(others))
    if HARD_CASE in failed_cases:
        # 验收③只把这一条定为硬失败；其余用例只做观察，避免演示判据被扩大。
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
