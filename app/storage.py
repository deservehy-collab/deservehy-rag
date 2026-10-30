from datetime import date

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.models import Conversation, ConversationSummary, Message, Ticket


def _history_filter(conversation_id: int):
    # JSON 列的 None 会落成 JSON `null` 而不是 SQL NULL，tool_calls 的过滤放到 Python 侧。
    return (
        Message.conversation_id == conversation_id,
        Message.role.in_(("user", "assistant")),
        Message.content.is_not(None),
        Message.content != "",
    )


def _is_final(row: Message) -> bool:
    return not row.tool_calls


class ConversationStore:
    def __init__(self, session_factory: sessionmaker[Session]):
        self.session_factory = session_factory

    def start_turn(
        self, *, user_id: str, message: str, conversation_id: int | None = None, with_message_id: bool = False
    ) -> int | tuple[int, int]:
        with self.session_factory() as session:
            conversation = session.get(Conversation, conversation_id) if conversation_id else None
            if conversation is None or conversation.user_id != user_id:
                conversation = Conversation(user_id=user_id)
                session.add(conversation)
                session.flush()
            else:
                conversation.updated_at = func.now()
            message_row = Message(conversation_id=conversation.id, role="user", content=message)
            session.add(message_row)
            session.commit()
            if with_message_id:
                return conversation.id, message_row.id
            return conversation.id

    def add_message(
        self,
        conversation_id: int,
        role: str,
        *,
        content: str | None = None,
        tool_calls: list[dict] | None = None,
        tool_call_id: str | None = None,
    ) -> int:
        with self.session_factory() as session:
            message = Message(
                conversation_id=conversation_id,
                role=role,
                content=content,
                tool_calls=tool_calls,
                tool_call_id=tool_call_id,
            )
            session.add(message)
            session.commit()
            return message.id

    def owner(self, conversation_id: int) -> str | None:
        with self.session_factory() as session:
            conversation = session.get(Conversation, conversation_id)
            return conversation.user_id if conversation else None

    def context_messages(self, conversation_id: int) -> list[dict]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(Message).where(*_history_filter(conversation_id)).order_by(Message.id)
            )
            return [{"id": row.id, "role": row.role, "content": row.content} for row in rows if _is_final(row)]

    def messages_between(self, conversation_id: int, from_msg_id: int, upto_msg_id: int) -> list[dict]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(Message).where(
                    *_history_filter(conversation_id),
                    Message.id > from_msg_id,
                    Message.id <= upto_msg_id,
                ).order_by(Message.id)
            )
            return [{"id": row.id, "role": row.role, "content": row.content} for row in rows if _is_final(row)]

    def summaries(self, conversation_id: int) -> list[dict]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(ConversationSummary)
                .where(ConversationSummary.conversation_id == conversation_id)
                .order_by(ConversationSummary.seq)
            )
            return [
                {"id": row.id, "seq": row.seq, "from_msg_id": row.from_msg_id,
                 "upto_msg_id": row.upto_msg_id, "content": row.content}
                for row in rows
            ]

    def anchors(self, conversation_id: int) -> tuple[int, int]:
        with self.session_factory() as session:
            conversation = session.get(Conversation, conversation_id)
            if conversation is None:
                return 0, 0
            return conversation.summary_upto_msg_id or 0, conversation.layer1_from_msg_id or 0

    def set_layer1_from(self, conversation_id: int, msg_id: int) -> None:
        with self.session_factory() as session:
            # 降级不是用户活动，不改 updated_at，避免会话列表顺序跳动。
            session.execute(
                update(Conversation)
                .where(Conversation.id == conversation_id)
                .values(layer1_from_msg_id=msg_id, updated_at=Conversation.updated_at)
            )
            session.commit()

    def append_summary(
        self, conversation_id: int, *, from_msg_id: int, upto_msg_id: int, content: str
    ) -> int | None:
        with self.session_factory() as session:
            conversation = session.get(Conversation, conversation_id)
            if conversation is None or (conversation.summary_upto_msg_id or 0) != from_msg_id:
                return None
            seq = (session.scalar(
                select(func.max(ConversationSummary.seq)).where(
                    ConversationSummary.conversation_id == conversation_id
                )
            ) or 0) + 1
            session.add(ConversationSummary(
                conversation_id=conversation_id, seq=seq, from_msg_id=from_msg_id,
                upto_msg_id=upto_msg_id, content=content,
            ))
            session.execute(
                update(Conversation)
                .where(Conversation.id == conversation_id)
                .values(summary_upto_msg_id=upto_msg_id, updated_at=Conversation.updated_at)
            )
            session.commit()
            return seq

    def list_conversations(self, user_id: str, limit: int = 50) -> list[dict]:
        first_ids = select(func.min(Message.id)).where(
            Message.conversation_id == Conversation.id, Message.role == "user"
        ).correlate(Conversation).scalar_subquery()
        first_content = select(Message.content).where(
            Message.id == first_ids
        ).scalar_subquery()
        summary_count = select(func.count(ConversationSummary.id)).where(
            ConversationSummary.conversation_id == Conversation.id
        ).correlate(Conversation).scalar_subquery()
        message_count = select(func.count(Message.id)).where(
            *_history_filter(Conversation.id)
        ).correlate(Conversation).scalar_subquery()
        with self.session_factory() as session:
            rows = session.execute(
                select(Conversation.id, first_content, Conversation.updated_at, summary_count, message_count)
                .where(Conversation.user_id == user_id)
                .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
                .limit(limit)
            )
            return [
                {
                    "id": row[0],
                    "title": " ".join((row[1] or "新对话").split())[:40],
                    "updated_at": row[2].isoformat(sep=" ", timespec="seconds") if row[2] else None,
                    "summarized": (row[3] or 0) > 0,
                    "message_count": row[4] or 0,
                }
                for row in rows
            ]

    def conversation_messages(self, conversation_id: int, user_id: str) -> list[dict] | None:
        with self.session_factory() as session:
            conversation = session.get(Conversation, conversation_id)
            if conversation is None or conversation.user_id != user_id:
                return None
            rows = session.scalars(
                select(Message).where(*_history_filter(conversation_id)).order_by(Message.id)
            )
            return [
                {
                    "id": row.id,
                    "role": row.role,
                    "content": row.content,
                    "created_at": row.created_at.isoformat(sep=" ", timespec="seconds")
                    if row.created_at else None,
                }
                for row in rows if _is_final(row)
            ]

    def create_ticket(self, conversation_id: int, description: str, ticket_type: str) -> str:
        day = date.today().strftime("%Y%m%d")
        for _ in range(3):
            with self.session_factory() as session:
                try:
                    ticket_no = self._next_ticket_no(session, day)
                    session.add(
                        Ticket(
                            ticket_no=ticket_no,
                            conversation_id=conversation_id,
                            description=description,
                            ticket_type=ticket_type,
                        )
                    )
                    session.commit()
                    return ticket_no
                except IntegrityError:
                    session.rollback()
        raise RuntimeError("工单号生成失败，请重试。")

    @staticmethod
    def _next_ticket_no(session: Session, day: str) -> str:
        latest = session.scalar(select(func.max(Ticket.ticket_no)).where(Ticket.ticket_no.like(f"T{day}%")))
        sequence = int(latest[-3:]) + 1 if latest else 1
        return f"T{day}{sequence:03d}"
