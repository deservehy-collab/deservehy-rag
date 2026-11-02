from app.topics.clean import clean_text, mask_pii


def test_masks_phone_id_card_and_order_numbers():
    assert mask_pii("电话 13812345678") == "电话 [手机号]"
    assert mask_pii("身份证 110101199001011234") == "身份证 [证件号]"
    assert mask_pii("订单 1001 什么时候发") == "订单[单号] 什么时候发"
    assert mask_pii("运单号：SF1234567890") == "运单号:[单号]"
    assert mask_pii("订单号 998877") == "订单号 [单号]"
    assert mask_pii("编号 12345678") == "编号 [单号]"
    assert mask_pii("电话１３８１２３４５６７８") == "电话[手机号]"
    assert mask_pii("银行卡号是6222021234567890") == "银行卡号是[单号]"
    assert mask_pii("电话1381234567890") == "电话[单号]"
    assert mask_pii("卡号－6222021234567890") == "卡号-[单号]"
    assert mask_pii("卡号_6222021234567890") == "卡号_[单号]"


def test_remasking_already_masked_text_is_fixed_point():
    text = "订单号 [单号] 电话[手机号] 证件[证件号]"
    assert mask_pii(text) == text


def test_keeps_short_numbers_that_are_not_ids():
    assert mask_pii("满 99 包邮吗") == "满 99 包邮吗"


def test_normalizes_fullwidth_and_whitespace():
    assert clean_text("  ＡＢＣ　１２３  ") == "ABC 123"
    assert clean_text("你好\u200b世界\n\n\t怎么 退货") == "你好世界 怎么 退货"
    assert clean_text('“退换货？”') == "退换货?"


def test_is_idempotent():
    once = clean_text(mask_pii("  订单 1001， 电话13812345678！！  "))
    twice = clean_text(mask_pii(once))
    assert once == twice == "订单[单号], 电话[手机号]!!"


def test_empty_input():
    assert clean_text(None) == ""
    assert clean_text("   ") == ""
