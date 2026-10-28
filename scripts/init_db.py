import argparse

from app.config import Settings
from app.database import build_database_url, build_engine, build_session_factory, ensure_database
from app.models import Base
from app.seed import seed_faq


def initialize(settings: Settings | None = None, *, include_test: bool = True) -> dict[str, int]:
    settings = settings or Settings.from_env()
    names = [settings.db_name] + ([settings.db_name_test] if include_test else [])
    seeded = {}
    for name in names:
        ensure_database(settings, name)
        engine = build_engine(build_database_url(settings, name))
        try:
            Base.metadata.create_all(engine)
            seeded[name] = seed_faq(build_session_factory(engine))
        finally:
            engine.dispose()
    return seeded


def main() -> int:
    parser = argparse.ArgumentParser(description="初始化 ch02 数据库：建库、建表、补 FAQ 种子，可重复执行。")
    parser.add_argument("--no-test", action="store_true", help="只初始化主库，不动测试库")
    args = parser.parse_args()
    for name, count in initialize(include_test=not args.no_test).items():
        print(f"{name}: 四张表已就绪，本次新增 FAQ {count} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
