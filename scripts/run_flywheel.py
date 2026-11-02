import argparse
import asyncio
import json
from pathlib import Path

from app.config import Settings
from app.database import build_database_url, build_engine, build_session_factory
from app.flywheel.pipeline import drain
from app.flywheel.standardize import standardize_batch
from app.model import build_model


async def _annotation(model, path: Path) -> int:
    samples = json.loads(path.read_text(encoding="utf-8"))
    rows = [(index, sample["raw"]) for index, sample in enumerate(samples)]
    got = {item.pool_id: item for item in await standardize_batch(model, rows, batch_size=10)}
    passed = 0
    for index, sample in enumerate(samples):
        item = got.get(index)
        question = item.normalized_question if item else ""
        # expected_any 是概念组：每组至少命中一个词，不是要求同义词全部出现。
        missing = [group for group in sample["expected_any"]
                   if not any(word in question for word in group)]
        banned = [word for word in sample.get("forbidden", []) if word in question]
        ok = not missing and not banned
        passed += ok
        print(f"{'PASS' if ok else 'FAIL'} {sample['id']} 「{sample['raw']}」→「{question}」"
              f"{' 缺:' + '|'.join('/'.join(group) for group in missing) if missing else ''}"
              f"{' 含禁词:' + ','.join(banned) if banned else ''}")
    rate = passed / len(samples) if samples else 0.0
    print(f"标注集 pass_rate={rate:.2f} ({passed}/{len(samples)})")
    return 0 if rate >= 0.8 else 1


async def _drain(factory, model, limit: int, batch_size: int, candidates: int) -> int:
    report = await drain(factory, model, limit=limit, batch_size=batch_size, candidates=candidates)
    print(f"待处理 {report.pending} 条，标准化 {report.standardized} 条，"
          f"归并 {report.merged} 条，新建待审 {report.created} 条，失败 {report.failed} 条。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="排空低置信问题池：标准化 → 查重 → 待审队列。")
    parser.add_argument("--limit", type=int, default=50, help="本次最多处理多少条池行")
    parser.add_argument("--batch-size", type=int, default=None, help="标准化每批条数，默认读配置")
    parser.add_argument("--candidates", type=int, default=None, help="查重候选上限，默认读配置")
    parser.add_argument("--standardize-only", action="store_true",
                        help="只跑标注集校验，不动数据库（配合 --annotation）")
    parser.add_argument("--annotation", type=str, default="evals/flywheel_standardize.json",
                        help="标注集路径")
    args = parser.parse_args()

    settings = Settings.from_env()
    model = build_model(settings)
    batch_size = args.batch_size or settings.flywheel_batch_size
    candidates = args.candidates or settings.flywheel_dedupe_candidates
    try:
        if args.standardize_only:
            return asyncio.run(_annotation(model, Path(args.annotation)))
        engine = build_engine(build_database_url(settings))
        factory = build_session_factory(engine)
        try:
            return asyncio.run(_drain(factory, model, args.limit, batch_size, candidates))
        finally:
            engine.dispose()
    finally:
        model.root_client.close()


if __name__ == "__main__":
    raise SystemExit(main())
