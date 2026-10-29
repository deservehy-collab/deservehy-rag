from app.knowledge.splitter import split_markdown


def test_heading_paths_become_category_and_section_path():
    text = """# 配送

简介。

## 运费

### 普通地区

满 99 元包邮，未满收 8 元。
"""
    chunks = split_markdown(text, content_type="policy")
    leaf = next(item for item in chunks if item.questions == "普通地区")
    assert leaf.category == "配送 › 运费"
    assert leaf.section_path == "配送 › 运费 › 普通地区"
    assert leaf.answer == "满 99 元包邮，未满收 8 元。"
    assert leaf.content_type == "policy"


def test_root_body_uses_document_title_as_category():
    text = "# 手册\n\n欢迎使用。\n\n## 第一节\n\n内容。\n"
    chunks = split_markdown(text, content_type="manual")
    intro = chunks[0]
    assert intro.questions == "手册"
    assert intro.category == "手册"
    assert "欢迎使用" in intro.answer


def test_key_clause_marker_sets_flag_and_is_stripped():
    text = "# 政策\n\n## 退货\n\n【关键条款】定制商品不支持退货。其他商品 7 天可退。\n"
    chunk = split_markdown(text, content_type="policy")[0]
    assert chunk.is_key_clause is True
    assert "【关键条款】" not in chunk.answer
    assert chunk.answer.startswith("定制商品不支持退货")


def test_long_section_splits_on_sentence_boundary_with_overlap():
    sentences = [f"第{i:02d}条说明：商品在签收后七天内可以申请退货。" for i in range(1, 31)]
    text = "# 政策\n\n## 长文\n\n" + "".join(sentences) + "\n"
    chunks = [item for item in split_markdown(text, content_type="policy") if item.questions == "长文"]
    assert len(chunks) >= 2
    assert all(len(item.answer) <= 500 for item in chunks)
    assert all(item.answer.endswith("。") for item in chunks)
    overlap = 0
    for size in range(len(chunks[1].answer), 0, -1):
        if chunks[0].answer.endswith(chunks[1].answer[:size]):
            overlap = size
            break
    assert overlap >= 20


def test_long_table_repeats_header_in_every_chunk():
    rows = [f"| 商品{i:02d} | {i} 元 | 现货 |" for i in range(1, 31)]
    text = (
        "# 售后\n\n## 价目表\n\n"
        "| 商品 | 价格 | 库存 |\n|---|---|---|\n" + "\n".join(rows) + "\n"
    )
    chunks = [item for item in split_markdown(text, content_type="policy") if item.questions == "价目表"]
    assert len(chunks) >= 2
    for item in chunks:
        assert item.answer.startswith("| 商品 | 价格 | 库存 |")
        assert "|---|---|---|" in item.answer.splitlines()[1]
        assert len(item.answer) <= 500
    joined = "\n".join(item.answer for item in chunks)
    assert all(f"商品{i:02d}" in joined for i in range(1, 31))


def test_headings_inside_code_fence_are_ignored():
    text = "# 手册\n\n## 代码\n\n```bash\n# 这是注释\n## 不是标题\necho hi\n```\n\n## 下一节\n\n正文。\n"
    chunks = split_markdown(text, content_type="manual")
    code = next(item for item in chunks if item.questions == "代码")
    assert "## 不是标题" in code.answer
    assert not any(item.questions == "不是标题" for item in chunks)
    assert any(item.questions == "下一节" for item in chunks)


def test_empty_document_returns_no_chunks():
    assert split_markdown("", content_type="policy") == []
    assert split_markdown("   \n\n", content_type="policy") == []
