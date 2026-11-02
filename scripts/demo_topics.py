"""ch10 三条验收一次跑完。"""

from __future__ import annotations

import json
from pathlib import Path

from app.config import Settings
from app.database import build_database_url, build_engine, build_session_factory
from app.topics.dataset import read_jsonl
from app.topics.evaluation import compute_metrics, top_confusions
from app.topics.model import ModelNotReady, TopicModel
from app.topics.stats import distribution
from app.topics.taxonomy import LABELS
from app.topics.worker import TopicClient, run_batch

MODEL = Path(".cache/models/topic-classifier")
TEST = Path(".cache/topics/splits/test.jsonl")


def main() -> int:
    if not (MODEL / "labels.json").exists() or not TEST.exists():
        print("缺少模型或测试集：先跑 --split 与 scripts/train_topic_classifier.py。")
        return 2
    try:
        model = TopicModel(MODEL)
    except ModelNotReady as exc:
        print(f"模型未就绪：{exc}")
        return 2
    test = read_jsonl(TEST)
    # TopicModel.predict 单次上限 64 条，测试集约 120 条，必须自己切块。
    predictions = []
    for start in range(0, len(test), 64):
        predictions.extend(model.predict([item.text for item in test[start:start + 64]]))
    metrics = compute_metrics(
        [[1 if label in item.labels else 0 for label in LABELS] for item in test],
        [[1 if label in prediction.labels else 0 for label in LABELS] for prediction in predictions],
    )
    print(f"① 测试集 macro-F1={metrics['macro_f1']} micro-F1={metrics['micro_f1']}")
    for label in LABELS:
        row = metrics["per_label"][label]
        print(f"   {label}: P={row['precision']} R={row['recall']} F1={row['f1']} "
              f"support={row['support']}")
    print("   Top 混淆对：", top_confusions(metrics["cooccurrence"], 5))

    settings = Settings.from_env()
    engine = build_engine(build_database_url(settings))
    factory = build_session_factory(engine)
    client = TopicClient(settings.topic_service_url)
    try:
        report = run_batch(factory, client, min_pending=1, limit=50, force=True)
        if report.failed and not report.classified:
            print("② 推理服务不可达：先双击 start-topic.bat。")
            return 2
        print(f"② 批量归类：待归类 {report.pending}，已归类 {report.classified}，失败 {report.failed}")
        data = distribution(factory)
        print(f"   已归类 {data['total_classified']}，多标签 {data['multi_label']}，"
              f"待归类 {data['pending']}")
    finally:
        client.close()
        engine.dispose()

    cases = [json.loads(line) for line in Path("evals/topics/multilabel_cases.jsonl")
             .read_text(encoding="utf-8").splitlines() if line.strip()]
    failures = []
    print("③ 多标签用例：")
    for case in cases:
        prediction = model.predict([case["text"]])[0]
        hit = set(case["expected"]) <= set(prediction.labels)
        print(f"   {'✓' if hit else '✗'} {case['text']} → {prediction.labels}")
        if not hit:
            failures.append(case["text"])
    others = [text for text in failures if text != "买大了想退"]
    if others:
        print("其他用例未全部命中（仅提示）：" + "、".join(others))
    if "买大了想退" in failures:
        # 验收③只把「买大了想退」定为硬失败，其余用例只观察，避免演示判据被扩大。
        print("多标签验收失败：买大了想退 没有同时命中退换货与尺码。")
        return 3
    print("后台主题分布页：http://127.0.0.1:8000/admin/topics")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
