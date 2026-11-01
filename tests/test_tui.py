from kuma_agent.llm import Message
from kuma_agent.tui import (
    display_width,
    format_log,
    is_quit,
    tail_lines,
    wrap_display,
)


def test_display_width_counts_wide_chars_as_two():
    assert display_width("abc") == 3
    assert display_width("クマ") == 4
    assert display_width("aク") == 3


def test_wrap_display_wraps_by_display_width():
    assert wrap_display("あいうえお", 4) == ["あい", "うえ", "お"]
    assert wrap_display("abcdef", 3) == ["abc", "def"]


def test_wrap_display_keeps_newlines():
    assert wrap_display("ab\ncd", 10) == ["ab", "cd"]
    assert wrap_display("", 10) == [""]


def test_format_log_shows_speakers_and_skips_tools():
    messages = [
        Message("user", "こんにちは"),
        Message("assistant", "", tool_calls=[{"id": "c1", "name": "now", "args": {}}]),
        Message("tool", "2026-11-02", tool_call_id="c1", tool_name="now"),
        Message("assistant", "やあ、元気？"),
    ]
    lines = format_log(messages, width=40)
    assert lines[0].startswith("[1] あなた: こんにちは")
    assert any(line.startswith("[2] クマ: やあ、元気？") for line in lines)
    assert all("2026-11-02" not in line for line in lines)


def test_format_log_wraps_long_lines_with_indent():
    lines = format_log([Message("user", "あ" * 30)], width=20)
    assert len(lines) > 1
    assert all(display_width(line) <= 20 for line in lines)
    assert lines[1].startswith(" ")


def test_is_quit():
    assert is_quit("/quit")
    assert is_quit("  /quit  ")
    assert not is_quit("quit")


def test_tail_lines():
    assert tail_lines(["a", "b", "c"], 2) == ["b", "c"]
    assert tail_lines(["a"], 0) == []
    assert tail_lines([], 3) == []
