import pytest

from app.topics.taxonomy import BOUNDARIES, DESCRIPTIONS, LABELS, OTHER, order_labels, validate_labels


def test_labels_are_17_unique_in_fixed_order():
    assert len(LABELS) == 17 == len(set(LABELS))
    assert LABELS[:3] == ("退换货", "物流", "尺码")
    assert LABELS[-1] == OTHER == "其他"
    assert set(DESCRIPTIONS) == set(LABELS)
    assert all(DESCRIPTIONS[label].strip() for label in LABELS)


def test_boundaries_cover_user_rules_verbatim():
    text = "".join(BOUNDARIES)
    for phrase in ("修归保修维修", "退归退换货", "运费管钱", "物流管货",
                   "价保是补差价", "优惠活动是券和满减"):
        assert phrase in text


def test_order_labels_dedupes_and_follows_taxonomy_order():
    assert order_labels(["尺码", "退换货", "尺码"]) == ["退换货", "尺码"]
    assert order_labels([]) == []


def test_validate_labels_rejects_empty_and_unknown():
    with pytest.raises(ValueError):
        validate_labels([])
    with pytest.raises(ValueError, match="未知主题标签"):
        validate_labels(["退款"])
    assert validate_labels(["评价", "退换货"]) == ["退换货", "评价"]


def test_multilabel_policy_example_is_expressible():
    assert order_labels(["退换货", "尺码"]) == ["退换货", "尺码"]
