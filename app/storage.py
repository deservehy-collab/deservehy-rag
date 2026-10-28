from datetime import date

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.models import Conversation, Faq, Message, Ticket

FAQ_LIMIT = 5


class ConversationStore:
    def __init__(self, session_factory: sessionmaker[Session]):
        self.session_factory = session_factory

    def start_turn(self, *, user_id: str, message: str, conversation_id: int | None = None) -> int:
        with self.session_factory() as session:
            conversation = session.get(Conversation, conversation_id) if conversation_id else None
            if conversation is None or conversation.user_id != user_id:
                conversation = Conversation(user_id=user_id)
                session.add(conversation)
                session.flush()
            session.add(Message(conversation_id=conversation.id, role="user", content=message))
            session.commit()
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

    def search_faq(self, keyword: str, limit: int = FAQ_LIMIT) -> list[dict]:
        keyword = keyword.strip()[:40]
        if not keyword:
            return []
        escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        statement = (
            select(Faq)
            .where(
                or_(
                    Faq.question.like(pattern, escape="\\"),
                    Faq.answer.like(pattern, escape="\\"),
                    Faq.category.like(pattern, escape="\\"),
                )
            )
            .order_by(Faq.id)
            .limit(limit)
        )
        with self.session_factory() as session:
            return [
                {"id": row.id, "question": row.question, "answer": row.answer, "category": row.category}
                for row in session.scalars(statement)
            ]
