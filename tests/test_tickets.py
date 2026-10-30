import sqlite3

from sqlalchemy import select

from app.models import Ticket
from tests.support import FixedClassifier, api_with_db, events


def complaint(client, client_id="anon-ticket-1"):
    response = client.post("/api/chat", json={"message": "我要投诉", "client_id": client_id})
    frames = events(response)
    conversation_id = frames[0][1]["conversation_id"]
    actions = [payload for name, payload in frames if name == "actions"][0]
    return conversation_id, actions["turn"]


def test_ticket_is_created_and_second_click_is_idempotent():
    with api_with_db(lambda request: None, classifier=FixedClassifier("投诉")) as (client, seen, factory):
        conversation_id, turn = complaint(client)
        body = {"conversation_id": conversation_id, "client_id": "anon-ticket-1", "turn": turn}
        first = client.post("/api/tickets", json=body)
        assert first.status_code == 200
        ticket_no = first.json()["ticket_no"]
        assert ticket_no.startswith("T") and first.json()["status"] == "待处理"
        second = client.post("/api/tickets", json=body)
        assert second.json()["ticket_no"] == ticket_no
        with factory() as session:
            rows = session.scalars(select(Ticket)).all()
            assert len(rows) == 1
            assert rows[0].ticket_type == "投诉"
            assert rows[0].description == "用户投诉：我要投诉"


def test_ticket_rejects_other_owner_and_stale_turn():
    with api_with_db(lambda request: None, classifier=FixedClassifier("投诉")) as (client, seen, factory):
        conversation_id, turn = complaint(client)
        forbidden = client.post("/api/tickets", json={
            "conversation_id": conversation_id, "client_id": "anon-ticket-9", "turn": turn})
        assert forbidden.status_code == 403
        assert forbidden.json()["code"] == "forbidden"
        stale = client.post("/api/tickets", json={
            "conversation_id": conversation_id, "client_id": "anon-ticket-1", "turn": turn + 1})
        assert stale.status_code == 409
        assert stale.json()["code"] == "no_pending_offer"


def test_ticket_requires_a_pending_offer():
    with api_with_db(lambda request: None, classifier=FixedClassifier("投诉")) as (client, seen, factory):
        conversation_id, turn = complaint(client)
        client.post("/api/tickets", json={"conversation_id": conversation_id,
                                          "client_id": "anon-ticket-1", "turn": turn})
        # 新一轮闲聊会清空 offer
        client.post("/api/chat", json={"message": "你好", "client_id": "anon-ticket-1",
                                       "conversation_id": conversation_id})
        stale = client.post("/api/tickets", json={"conversation_id": conversation_id,
                                                  "client_id": "anon-ticket-1", "turn": turn})
        assert stale.status_code == 409


def test_ticket_input_is_validated():
    with api_with_db(lambda request: None) as (client, seen, factory):
        assert client.post("/api/tickets", json={"conversation_id": 0, "client_id": "x", "turn": 0}).status_code == 422


def test_ticket_survives_checkpoint_write_failure():
    with api_with_db(lambda request: None, classifier=FixedClassifier("投诉")) as (client, seen, factory):
        conversation_id, turn = complaint(client)
        body = {"conversation_id": conversation_id, "client_id": "anon-ticket-1", "turn": turn}
        workflow = client.app.state.workflow

        async def broken(*args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")

        workflow.update_state = broken
        first = client.post("/api/tickets", json=body)
        assert first.status_code == 200
        ticket_no = first.json()["ticket_no"]
        # 回写失败后重试：进程内缓存保证不会再建第二张。
        second = client.post("/api/tickets", json=body)
        assert second.json()["ticket_no"] == ticket_no
        with factory() as session:
            assert len(session.scalars(select(Ticket)).all()) == 1
