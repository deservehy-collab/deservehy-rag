import argparse
from datetime import datetime
from pathlib import Path

from app.config import Settings
from app.database import build_database_url, build_engine, build_session_factory
from app.knowledge.evalstore import load_runs

ROOT = Path(__file__).resolve().parents[1]
METRICS = ("recall@5", "recall@10", "mrr@10", "refusal_rate", "faithfulness")
HEADERS = ("Recall@5", "Recall@10", "MRR@10", "不可答拒答率", "Faithfulness")
# 五个指标都是越高越好：refusal_rate 是 summarize() 只在「不可答」题上算的拒答率
# （app/knowledge/metrics.py:24），不可答就该拒，所以高 = 好。


def compare(previous: dict | None, current: dict) -> dict[str, str]:
    if previous is None:
        return {name: "" for name in METRICS}
    deltas = {}
    for name in METRICS:
        before, after = previous.get(name), current.get(name)
        if before is None or after is None:
            deltas[name] = "n/a"          # 「这轮没测」不算下滑
            continue
        delta = after - before
        if abs(delta) < 5e-4:
            deltas[name] = "=0.000 ="
        else:
            deltas[name] = f"{delta:+.3f} {'↑' if delta > 0 else '↓'}"
    return deltas


def declined(deltas: dict[str, str]) -> bool:
    return any(value.endswith("↓") for value in deltas.values())


def render(rows: list[dict]) -> str:
    if not rows:
        return "还没有评估记录。先跑 `python scripts/evaluate_retrieval.py --no-faith`。"
    lines = ["# ch09 检索评估趋势", "",
             f"- 生成时间：{datetime.now().isoformat(timespec='seconds')}",
             f"- 共 {len(rows)} 轮；箭头向下 = 比上一轮下滑，行首带双叹号 = 这一轮有指标在掉；"
             "`n/a` = 任一侧没测（`--no-faith`）。", "",
             "| 轮次 | 时间 | 触发 | 题数 | confidence_min | " + " | ".join(HEADERS) + " |",
             "|---|---|---|---|---|" + "---|" * len(HEADERS)]
    previous = None
    for row in rows:
        metrics = row["metrics"]
        deltas = compare(previous, metrics)
        flag = "!! " if declined(deltas) else ""
        cells = []
        for name in METRICS:
            value = metrics.get(name)
            shown = "n/a" if value is None else f"{value:.3f}"
            delta = deltas[name]
            cells.append(f"{shown}<br>{delta}" if delta else shown)
        floor = metrics.get("confidence_min")
        lines.append(f"| {flag}{row['id']} | {row.get('created_at') or '-'} | {row['triggered_by']} "
                     f"| {row['dataset_size']} | {'-' if floor is None else f'{floor:.3f}'} | "
                     + " | ".join(cells) + " |")
        previous = metrics
    return "\n".join(lines) + "\n"


def _positive_limit(text: str) -> int:
    """--limit 至少 1：0 会查出空表被当成「还没跑过」，-1 在 SQL limit 里行为也不直观。"""
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("--limit 必须 >= 1")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="对比最近几轮检索评估指标，下滑一眼可见。")
    parser.add_argument("--limit", type=_positive_limit, default=10, help="看最近多少轮（>= 1）")
    parser.add_argument("--report-dir", default="evals/reports", help="报告输出目录")
    args = parser.parse_args()

    settings = Settings.from_env()
    engine = build_engine(build_database_url(settings))
    try:
        rows = load_runs(build_session_factory(engine), limit=args.limit)
    finally:
        engine.dispose()
    text = render(rows)
    print(text)
    if not rows:
        # 没记录就是没验收过，别用「空表」冒充成功；验收⑥要求至少两轮。
        return 1
    directory = Path(args.report_dir)
    if not directory.is_absolute():
        directory = ROOT / directory
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = directory / f"ch09-eval-trend-{stamp}.md"
    path.write_text(text, encoding="utf-8")
    print(f"报告：{path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
