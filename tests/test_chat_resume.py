import asyncio
from contextlib import contextmanager

from sqlalchemy import func, select

from app.models import Ticket, ToolAuditLog
from app.prompts import TICKET_CANCELLED_TEXT, TICKET_CREATED_TEXT
from app.schemas import ResumeRequest
from app.service import CustomerService
from tests.support import api_with_db, events, streamed_tool_call

TICKET_CALL = {"description": "保温杯收到就是漏的，想退货", "ticket_type": "售后"}
CLIENT = "anon-resume-1"


@contextmanager
def confirm_card(handler, client_id=CLIENT):
    """跑一轮到挂起，交出 (client, factory, 会话号, 卡片, 上游调用)。会话号取 meta 帧，不给 confirm 加字段。"""
    with api_with_db(handler) as (client, seen, factory):
        frames = events(client.post("/api/chat", json={"message": "帮我建个工单，杯子是漏的",
                                                       "client_id": client_id}))
        assert [name for name, _ in frames][-2:] == ["confirm", "done"]
        assert len(seen) == 1                                  # 暂停轮没有第二次模型调用
        card = next(payload for name, payload in frames if name == "confirm")
        yield client, factory, frames[0][1]["conversation_id"], card, seen


def test_accept_creates_exactly_one_ticket_and_streams_the_reply():
    def handler(request):
        return streamed_tool_call("create_ticket", TICKET_CALL)

    with confirm_card(handler) as (client, factory, conversation_id, card, seen):
        frames = events(client.post("/api/chat/resume", json={
            "conversation_id": conversation_id, "client_id": CLIENT,
            "decision": "accept", "pending_id": card["pending_id"]}))
        assert [name for name, _ in frames] == ["status", "status", "token", "done"]
        with factory() as session:
            tickets = list(session.scalars(select(Ticket)))
            audit = list(session.scalars(select(ToolAuditLog)))
        assert len(tickets) == 1
        assert frames[2][1]["content"] == TICKET_CREATED_TEXT.format(ticket_no=tickets[0].ticket_no)
        assert (audit[0].status, audit[0].tool_name) == ("成功", "create_ticket")
        assert len(seen) == 1                                   # resume 不再叫模型，只跑那一次写


def test_cancel_leaves_no_ticket_and_audits_a_denial():
    """验收⑤的接口形状：点取消 → 工单没建、审计状态「权限拒绝」。"""
    def handler(request):
        return streamed_tool_call("create_ticket", TICKET_CALL)

    with confirm_card(handler) as (client, factory, conversation_id, card, seen):
        frames = events(client.post("/api/chat/resume", json={
            "conversation_id": conversation_id, "client_id": CLIENT,
            "decision": "cancel", "pending_id": card["pending_id"]}))
        assert [name for name, _ in frames] == ["token", "done"]
        assert frames[0][1]["content"] == TICKET_CANCELLED_TEXT
        with factory() as session:
            assert session.scalar(select(func.count()).select_from(Ticket)) == 0
            audit = list(session.scalars(select(ToolAuditLog)))
        assert (audit[0].status, audit[0].retry_count) == ("权限拒绝", 0)


def test_a_stale_pending_id_is_refused_without_touching_the_graph():
    def handler(request):
        return streamed_tool_call("create_ticket", TICKET_CALL)

    with confirm_card(handler) as (client, factory, conversation_id, _, seen):
        frames = events(client.post("/api/chat/resume", json={
            "conversation_id": conversation_id, "client_id": CLIENT,
            "decision": "accept", "pending_id": "deadbeefdeadbeef"}))
        assert [name for name, _ in frames] == ["error"]
        assert frames[0][1]["code"] == "no_pending_confirm"
        with factory() as session:
            assert session.scalar(select(func.count()).select_from(Ticket)) == 0
            assert list(session.scalars(select(ToolAuditLog))) == []


def test_resuming_the_same_card_twice_creates_one_ticket():
    def handler(request):
        return streamed_tool_call("create_ticket", TICKET_CALL)

    with confirm_card(handler) as (client, factory, conversation_id, card, seen):
        payload = {"conversation_id": conversation_id, "client_id": CLIENT,
                   "decision": "accept", "pending_id": card["pending_id"]}
        events(client.post("/api/chat/resume", json=payload))
        second = events(client.post("/api/chat/resume", json=payload))
        assert [name for name, _ in second] == ["error"]
        assert second[0][1]["code"] == "no_pending_confirm"
        with factory() as session:
            assert len(list(session.scalars(select(Ticket)))) == 1


def test_another_user_cannot_resume_my_card():
    def handler(request):
        return streamed_tool_call("create_ticket", TICKET_CALL)

    with confirm_card(handler, "anon-resume-5") as (client, _, conversation_id, card, _):
        response = client.post("/api/chat/resume", json={
            "conversation_id": conversation_id, "client_id": "someone-else-1",
            "decision": "accept", "pending_id": card["pending_id"]})
        assert response.status_code == 403
        assert response.json()["code"] == "forbidden"


def test_resume_is_serialised_behind_the_conversation_lock():
    """「查卡片 → resume」必须与新一轮对话共用同一把锁：卡片在读与执行之间不许被别的一轮换掉。"""
    def handler(request):
        return streamed_tool_call("create_ticket", TICKET_CALL)

    async def collect(stream):
        return [item async for item in stream]

    with confirm_card(handler) as (client, factory, conversation_id, card, seen):
        service = client.app.state.service
        request = ResumeRequest(conversation_id=conversation_id, client_id=CLIENT,
                                decision="accept", pending_id=card["pending_id"])

        async def hold_and_resume():
            lock = service.workflow.lock(conversation_id)
            await lock.acquire()
            task = asyncio.create_task(collect(service.respond_resume(request)))
            await asyncio.sleep(0.05)
            finished_early = task.done()
            lock.release()
            return finished_early, await task

        finished_early, frames = client.portal.call(hold_and_resume)
        assert finished_early is False
        assert [name for name, _ in frames] == ["status", "status", "token", "done"]
        with factory() as session:
            assert len(list(session.scalars(select(Ticket)))) == 1


def test_decision_and_pending_id_are_validated():
    with api_with_db(lambda request: streamed_tool_call("create_ticket", TICKET_CALL)) as (client, seen, factory):
        bad = client.post("/api/chat/resume", json={"conversation_id": 1, "client_id": "anon-resume-6",
                                                    "decision": "maybe", "pending_id": "aaaaaaaa"})
        short = client.post("/api/chat/resume", json={"conversation_id": 1, "client_id": "anon-resume-6",
                                                      "decision": "accept", "pending_id": "abc"})
        assert bad.status_code == short.status_code == 422
        assert bad.json()["code"] == "invalid_request"
        assert seen == []


def test_the_session_lock_outlives_the_feeder_unwinding():
    """断连时放锁必须等 feeder 把栈解完：锁一放，同一会话的下一个请求就能和这一轮叠着跑；
    被取消那一轮没写 checkpoint、pending_write 还在，第二次 resume 会把同一次确认执行两遍（评审 I4）。"""
    order = []

    class Recorder:
        async def acquire(self):
            pass

        def release(self):
            order.append("release")

    class StubWorkflow:
        def lock(self, conversation_id):
            return Recorder()

    async def stream():
        try:
            for _ in range(10):
                await asyncio.sleep(0)
                yield "token", {"content": "x"}
        finally:
            await asyncio.sleep(0)              # 图内 finally 的 aclose() 还要再走几轮迭代
            order.append("feeder-done")

    async def scenario():
        service = CustomerService.__new__(CustomerService)
        service.workflow = StubWorkflow()
        frames = service._stream(1, stream())
        await frames.__anext__()
        await frames.aclose()                   # 客户端断开：消费侧就地收尾

    asyncio.run(scenario())
    assert order == ["feeder-done", "release"]
