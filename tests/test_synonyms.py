from app.knowledge.synonyms import expand_for_bm25


def test_expands_hit_terms_without_duplicating():
    expanded = expand_for_bm25("邮费怎么算")
    assert expanded.startswith("邮费怎么算")
    assert "运费" in expanded and "快递费" in expanded


def test_leaves_unknown_query_unchanged():
    assert expand_for_bm25("你好") == "你好"


def test_expansion_is_capped():
    assert len(expand_for_bm25("退货退款邮费发票保修").split()) <= 7
