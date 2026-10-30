from app.agent.guards import ACTION_LABELS, ActionGuard, AgentStream


def test_action_marker_is_removed_and_recorded():
    guard = ActionGuard()
    assert guard.feed("你可以选择下面的操作。[[actions:human,ticket]]") == "你可以选择下面的操作。"
    assert guard.actions == ["human", "ticket"]
    assert guard.flush() == ""


def test_split_action_marker_never_leaks():
    guard = ActionGuard()
    assert guard.feed("先这样[[act") == "先这样"
    assert guard.feed("ions:ticket]]") == ""
    assert guard.actions == ["ticket"]
    assert guard.flush() == ""


def test_citation_and_single_bracket_are_untouched():
    guard = ActionGuard()
    assert guard.feed("政策见[1]，标题是【A】。") == "政策见[1]，标题是【A】。"
    assert guard.flush() == ""


def test_duplicate_actions_are_deduped():
    guard = ActionGuard()
    guard.feed("[[actions:human]][[actions:human,ticket]]")
    assert guard.actions == ["human", "ticket"]


def test_lone_bracket_at_end_is_kept():
    guard = ActionGuard()
    assert guard.feed("见附录[") == "见附录"
    assert guard.flush() == "["


def test_truncated_marker_is_dropped_and_never_reemitted():
    """[[ 在本项目里只作动作标记的开括号；半截且始终不闭合的尾巴按协议垃圾丢弃，不回吐给正文。"""
    guard = ActionGuard()
    assert guard.feed("见 [[a") == "见 "
    assert guard.flush() == ""
    assert guard.actions == []

    longer = ActionGuard()
    head = longer.feed("政策[[actions:human")
    tail = longer.flush()
    assert head == "政策"
    assert tail == ""
    assert "[[" not in head + tail
    assert longer.actions == []


def test_agent_stream_strips_actions_and_invalid_citations():
    stream = AgentStream()
    stream.enable_citations({1})
    assert stream.feed("G500 不适用 B750[1][9]。") == "G500 不适用 B750[1]。"
    assert stream.feed("[[actions:human]]") == ""
    assert stream.finish() == ""
    assert stream.text == "G500 不适用 B750[1]。"
    assert stream.actions.actions == ["human"]
    assert ACTION_LABELS == {"human": "转人工", "ticket": "建工单"}


def test_agent_stream_falls_back_when_leaked_markup_blocks_text():
    stream = AgentStream()
    assert stream.feed("<｜｜DSM") == ""
    assert stream.finish() == ""
    assert stream.blocked is True


def test_agent_stream_with_empty_valid_set_strips_no_citation():
    stream = AgentStream()
    stream.enable_citations(set())
    assert stream.feed("退货政策见[1]，保修见[2]。") == "退货政策见[1]，保修见[2]。"
    assert stream.finish() == ""
    assert stream.text == "退货政策见[1]，保修见[2]。"
