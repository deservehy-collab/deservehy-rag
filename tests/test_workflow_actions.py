from app.workflow.actions import actions_payload, build_offer


def test_ticket_offer_for_complaint():
    offer = build_offer(3, ["human", "ticket"], "你们客服态度太差了", "投诉")
    assert offer == {
        "turn": 3,
        "actions": ["human", "ticket"],
        "ticket": {"type": "投诉", "description": "用户投诉：你们客服态度太差了"},
        "ticket_no": None,
        "refund": None,
        "refund_no": None,
    }


def test_ticket_type_by_intent():
    assert build_offer(1, ["ticket"], "怎么退货", "退款退货")["ticket"]["type"] == "售后"
    assert build_offer(1, ["ticket"], "什么时候发货", "订单")["ticket"]["type"] == "咨询"
    assert build_offer(1, ["human"], "你好", "闲聊")["ticket"] is None


def test_actions_payload_drops_unknown_ids():
    payload = actions_payload(["human", "followup", "ticket"], 2)
    assert payload == {"items": [{"id": "human", "label": "转人工"},
                                 {"id": "ticket", "label": "建工单"}], "turn": 2}


from app.workflow.actions import REFUND_REASONS


def test_offer_carries_refund_metadata_when_order_present():
    order = {"order_id": "1001", "status": "已发货", "amount": "199.00"}
    offer = build_offer(3, ["refund"], "这个能退吗", "退款退货", order=order)
    assert offer["refund"] == {"order_id": "1001", "reasons": list(REFUND_REASONS)}
    assert offer["ticket"] is None and offer["refund_no"] is None
    payload = actions_payload(["refund"], 3, offer["refund"])
    assert payload["items"] == [{"id": "refund", "label": "申请退款"}]
    assert payload["refund"]["order_id"] == "1001"


def test_offer_without_order_has_no_refund():
    assert build_offer(1, ["human"], "我要投诉", "投诉")["refund"] is None
