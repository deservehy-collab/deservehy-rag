"""手动批量归类：与后台调度共用 run_batch。"""

from __future__ import annotations

import argparse

from app.config import Settings
from app.database import build_database_url, build_engine, build_session_factory
from app.topics.worker import TopicClient, run_batch


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="ch10 手动批量主题归类")
    parser.add_argument("--min-pending", type=int, default=1)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    engine = build_engine(build_database_url(settings))
    client = TopicClient(settings.topic_service_url)
    try:
        report = run_batch(build_session_factory(engine), client,
                           min_pending=args.min_pending, limit=args.limit, force=args.force)
    finally:
        client.close()
        engine.dispose()
    print(f"待归类 {report.pending}，已归类 {report.classified}，失败 {report.failed}")
    if report.failed and not report.classified:
        print("推理服务不可达：请先双击 start-topic.bat。")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
