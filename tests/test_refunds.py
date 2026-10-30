from sqlalchemy import select

from app.models import Ticket
from app.workflow.actions import REFUND_REASONS
from tests.support import api_with_db


def make_conversation(client, user_id="anon-refund-1"):
    store = client.app.state.service.store
    return store.start_turn(user_id=user_id, message="这个能退吗")


def seed_offer(client, conversation_id, turn=1, order="1001"):
    workflow = client.app.state.workflow

    async def seed():
        await workflow.update_state(conversation_id, {"offer": {
            "turn": turn, "actions": ["refund"], "ticket": None, "ticket_no": None,
            "refund": {"order_id": order, "reasons": list(REFUND_REASONS)}, "refund_no": None}})

    client.portal.call(seed)


def test_refund_creates_one_ticket_and_is_idempotent():
    with api_with_db(lambda request: None) as (client, seen, factory):
        conversation_id = make_conversation(client)
        seed_offer(client, conversation_id)
        body = {"conversation_id": conversation_id, "client_id": "anon-refund-1", "turn": 1,
                "reason": REFUND_REASONS[0], "note": "杯口有裂纹"}
        first = client.post("/api/refunds", json=body)
        assert first.status_code == 200
        refund_no = first.json()["refund_no"]
        assert refund_no.startswith("T") and first.json()["status"] == "待处理"
        second = client.post("/api/refunds", json=body)
        assert second.json()["refund_no"] == refund_no
        with factory() as session:
            rows = session.scalars(select(Ticket)).all()
            assert len(rows) == 1
            assert rows[0].ticket_type == "售后"
            assert "退款申请：订单 1001" in rows[0].description
            assert "杯口有裂纹" in rows[0].description


def test_refund_rejects_unknown_reason():
    with api_with_db(lambda request: None) as (client, seen, factory):
        conversation_id = make_conversation(client)
        seed_offer(client, conversation_id)
        response = client.post("/api/refunds", json={
            "conversation_id": conversation_id, "client_id": "anon-refund-1", "turn": 1,
            "reason": "随便写写"})
        assert response.status_code == 422
        with factory() as session:
            assert session.scalars(select(Ticket)).all() == []


def test_refund_rejects_stale_offer_and_other_owner():
    with api_with_db(lambda request: None) as (client, seen, factory):
        conversation_id = make_conversation(client)
        seed_offer(client, conversation_id)
        stale = client.post("/api/refunds", json={
            "conversation_id": conversation_id, "client_id": "anon-refund-1", "turn": 2,
            "reason": REFUND_REASONS[0]})
        assert stale.status_code == 409
        other = client.post("/api/refunds", json={
            "conversation_id": conversation_id, "client_id": "anon-refund-9", "turn": 1,
            "reason": REFUND_REASONS[0]})
        assert other.status_code == 403
