from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, JSON, String, Text, func
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

IdType = mysql.BIGINT(unsigned=True).with_variant(Integer, "sqlite")
TABLE_OPTIONS = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}


class Base(DeclarativeBase):
    pass


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (Index("idx_user_id", "user_id"), TABLE_OPTIONS)

    id: Mapped[int] = mapped_column(IdType, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        Enum("进行中", "已转人工", "已结束", name="conversation_status"),
        nullable=False,
        default="进行中",
        server_default="进行中",
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class Message(Base):
    __tablename__ = "messages"
    # SQLite 索引名全库唯一，MySQL 允许两张表同名；ORM 用唯一名以便同一套模型建 SQLite 测试库。
    __table_args__ = (Index("idx_messages_conversation_id", "conversation_id"), TABLE_OPTIONS)

    id: Mapped[int] = mapped_column(IdType, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(
        IdType, ForeignKey("conversations.id", name="fk_messages_conversation"), nullable=False
    )
    role: Mapped[str] = mapped_column(Enum("user", "assistant", "tool", name="message_role"), nullable=False)
    content: Mapped[str | None] = mapped_column(Text)
    tool_calls: Mapped[list[dict] | None] = mapped_column(JSON)
    tool_call_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Faq(Base):
    __tablename__ = "faq"
    __table_args__ = (Index("idx_category", "category"), TABLE_OPTIONS)

    id: Mapped[int] = mapped_column(IdType, primary_key=True, autoincrement=True)
    question: Mapped[str] = mapped_column(String(512), nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class Ticket(Base):
    __tablename__ = "tickets"
    __table_args__ = (Index("idx_tickets_conversation_id", "conversation_id"), TABLE_OPTIONS)

    ticket_no: Mapped[str] = mapped_column(String(32), primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        IdType, ForeignKey("conversations.id", name="fk_tickets_conversation"), nullable=False
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    ticket_type: Mapped[str] = mapped_column(Enum("售后", "投诉", "咨询", name="ticket_type"), nullable=False)
    status: Mapped[str] = mapped_column(
        Enum("待处理", "已处理", name="ticket_status"), nullable=False, default="待处理", server_default="待处理"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
