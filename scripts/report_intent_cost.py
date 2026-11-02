"""按意图汇总 token 花销（数据来自自部署 Langfuse）。

    python -m scripts.report_intent_cost
    python -m scripts.report_intent_cost --from 2026-11-01T00:00:00+00:00 \
        --to 2026-11-03T00:00:00+00:00 --out evals/reports/ch09-intent-cost.md

没数据时退出码 1：验收要的是「真的能从 Langfuse 汇总出非零 token」，不是拿 0 糊过去。
"""

import argparse
import asyncio
from datetime import datetime
from pathlib import Path

from app.config import Settings
from app.observability.cost_report import collect, render_markdown

ROOT = Path(__file__).resolve().parents[1]


async def _run(settings, since, until):
    rows, mode, note = await collect(settings, since=since, until=until)
    return render_markdown(rows, since=since or "最近 7 天", until=until or "现在",
                           mode=mode, note=note), rows


def _output_path(args) -> Path:
    if args.out:
        path = Path(args.out)
        return path if path.is_absolute() else ROOT / path
    directory = Path(args.report_dir)
    if not directory.is_absolute():
        directory = ROOT / directory
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return directory / f"ch09-intent-cost-{stamp}.md"


def main() -> int:
    parser = argparse.ArgumentParser(description="按意图汇总 token 花销（数据来自自部署 Langfuse）。")
    parser.add_argument("--from", dest="since", default=None, help="起始时间，ISO8601；默认 7 天前")
    parser.add_argument("--to", dest="until", default=None, help="结束时间，ISO8601；默认现在")
    parser.add_argument("--out", default=None, help="报告文件的完整路径；不填则用 --report-dir 自动命名")
    parser.add_argument("--report-dir", default="evals/reports", help="报告输出目录（未给 --out 时生效）")
    args = parser.parse_args()

    settings = Settings.from_env()
    try:
        text, rows = asyncio.run(_run(settings, args.since, args.until))
    except ValueError as exc:
        # --from/--to 写错时给一行能看懂的说明，而不是 traceback。
        print(f"参数有误：{exc}")
        return 2
    if not rows:
        print(text)
        return 1                      # 没数据就是没验收过，别用 0 糊过去
    for row in rows:
        print(f"{row.intent:6s} 请求 {row.requests:4d} 输入 {row.input_tokens:7d} "
              f"输出 {row.output_tokens:7d} 合计 {row.total_tokens:7d} 花费 {row.cost:.4f}")
    path = _output_path(args)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    try:
        shown = path.relative_to(ROOT)
    except ValueError:
        shown = path
    print(f"报告：{shown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
