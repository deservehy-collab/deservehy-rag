from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

IdType = mysql.BIGINT(unsigned=True).with_variant(Integer, "sqlite")
CountType = mysql.INTEGER(unsigned=True).with_variant(Integer, "sqlite")
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


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"
    # SQLite 索引名全库唯一，索引名加表前缀；MySQL 的 DDL 仍用 idx_category / idx_vectorize_status。
    __table_args__ = (
        Index("idx_knowledge_category", "category"),
        Index("idx_knowledge_vectorize_status", "vectorize_status"),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(IdType, primary_key=True, autoincrement=True)
    category: Mapped[str] = mapped_column(String(255), nullable=False)
    questions: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    section_path: Mapped[str | None] = mapped_column(String(512))
    content_type: Mapped[str | None] = mapped_column(String(32))
    is_key_clause: Mapped[bool] = mapped_column(default=False, server_default="0", nullable=False)
    prev_chunk_id: Mapped[int | None] = mapped_column(
        IdType, ForeignKey("knowledge_chunks.id", name="fk_knowledge_prev", ondelete="SET NULL")
    )
    next_chunk_id: Mapped[int | None] = mapped_column(
        IdType, ForeignKey("knowledge_chunks.id", name="fk_knowledge_next", ondelete="SET NULL")
    )
    vector_id: Mapped[str | None] = mapped_column(String(64))
    vectorize_status: Mapped[str] = mapped_column(
        Enum("pending", "done", name="vectorize_status"),
        nullable=False,
        default="pending",
        server_default="pending",
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class QaExtractionStaging(Base):
    __tablename__ = "qa_extraction_staging"
    __table_args__ = (
        Index("idx_staging_batch_no", "batch_no"),
        Index("idx_staging_status", "status"),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(IdType, primary_key=True, autoincrement=True)
    batch_no: Mapped[str] = mapped_column(String(64), nullable=False)
    source_ref: Mapped[str | None] = mapped_column(String(255))
    question: Mapped[str | None] = mapped_column(Text)
    answer: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        Enum("extracted", "kept", "discarded", name="staging_status"),
        nullable=False,
        default="extracted",
        server_default="extracted",
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class LowConfidenceQuestion(Base):
    __tablename__ = "low_confidence_questions"
    __table_args__ = (
        Index("idx_low_confidence_source", "source"),
        Index("idx_low_confidence_created_at", "created_at"),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(IdType, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int | None] = mapped_column(
        IdType, ForeignKey("conversations.id", name="fk_lcq_conversation"), nullable=True
    )
    raw_question: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(
        Enum("retrieval_low_conf", "self_check", "user_feedback", name="low_confidence_source"), nullable=False
    )
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=True, server_default=func.now())


class FaithCase(Base):
    __tablename__ = "faith_cases"
    __table_args__ = (
        UniqueConstraint("eval_id", name="uk_eval_id"),
        Index("idx_faith_status", "status"),
        Index("idx_faith_last_seen_at", "last_seen_at"),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(IdType, primary_key=True, autoincrement=True)
    eval_id: Mapped[str] = mapped_column(String(16), nullable=False)
    bucket: Mapped[str] = mapped_column(String(24), nullable=False)
    query: Mapped[str] = mapped_column(String(512), nullable=False)
    strategy: Mapped[str] = mapped_column(String(24), nullable=False, default="hybrid_rerank", server_default="hybrid_rerank")
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    citations: Mapped[list[dict] | None] = mapped_column(JSON)
    judge_model: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(
        Enum("未解决", "已解决", "无需解决", name="faith_case_status"),
        nullable=False, default="未解决", server_default="未解决",
    )
    seen_count: Mapped[int] = mapped_column(CountType, nullable=False, default=1, server_default="1")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=True, server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=True, server_default=func.now())
    resolution: Mapped[str | None] = mapped_column(String(300))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)
