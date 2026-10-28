import re

from sqlalchemy import URL, create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings

_NAME = re.compile(r"[A-Za-z0-9_]+")


def validate_database_name(name: str) -> str:
    if not _NAME.fullmatch(name):
        raise ValueError("数据库名只能包含字母、数字和下划线。")
    return name


def build_database_url(settings: Settings, database: str | None = None) -> URL:
    return URL.create(
        "mysql+pymysql",
        username=settings.db_user,
        password=settings.db_password.get_secret_value(),
        host=settings.db_host,
        port=settings.db_port,
        database=validate_database_name(database or settings.db_name),
        query={"charset": "utf8mb4"},
    )


def build_engine(url: URL | str) -> Engine:
    return create_engine(url, pool_pre_ping=True, pool_recycle=3600)


def build_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


def ensure_database(settings: Settings, database: str) -> None:
    name = validate_database_name(database)
    server = create_engine(build_database_url(settings, database="mysql"), isolation_level="AUTOCOMMIT")
    try:
        with server.connect() as connection:
            connection.execute(
                text(f"CREATE DATABASE IF NOT EXISTS `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci")
            )
    finally:
        server.dispose()
