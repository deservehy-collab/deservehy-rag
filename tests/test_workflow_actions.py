from app.workflow.actions import actions_payload, build_offer


def test_ticket_offer_for_complaint():
    offer = build_offer(3, ["human", "ticket"], "你们客服态度太差了", "投诉")
    assert offer == {
        "turn": 3,
        "actions": ["human", "ticket"],
        "ticket": {"type": "投诉", "description": "用户投诉：你们客服态度太差了"},
        "ticket_no": None,
    }


def test_ticket_type_by_intent():
    assert build_offer(1, ["ticket"], "怎么退货", "退款退货")["ticket"]["type"] == "售后"
    assert build_offer(1, ["ticket"], "什么时候发货", "订单")["ticket"]["type"] == "咨询"
    assert build_offer(1, ["human"], "你好", "闲聊")["ticket"] is None


def test_actions_payload_drops_unknown_ids():
    payload = actions_payload(["human", "followup", "ticket"], 2)
    assert payload == {"items": [{"id": "human", "label": "转人工"},
                                 {"id": "ticket", "label": "建工单"}], "turn": 2}
