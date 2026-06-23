# Copyright (C) 2025 Luis González Fernández
# SPDX-License-Identifier: GPL-3.0-or-later

from agents_discussion.tools import _MAX_OUT, _truncate


def test_short_text_unchanged() -> None:
    text = "x" * (_MAX_OUT - 1)
    assert _truncate(text) == text


def test_exact_limit_unchanged() -> None:
    text = "x" * _MAX_OUT
    assert _truncate(text) == text


def test_long_text_keeps_head_and_tail() -> None:
    head_part = "H" * 5_000
    tail_part = "T" * 5_000
    text = head_part + tail_part
    out = _truncate(text)

    head = (_MAX_OUT * 2) // 3
    tail = _MAX_OUT - head
    assert out.startswith(text[:head])
    assert out.endswith(text[-tail:])
    assert "[truncated —" in out
    # Total budget respected (limit + marker line)
    assert len(out) <= _MAX_OUT + 80


def test_marker_reports_omitted_chars() -> None:
    text = "a" * 10_000
    out = _truncate(text)
    head = (_MAX_OUT * 2) // 3
    tail = _MAX_OUT - head
    omitted = 10_000 - head - tail
    assert f"{omitted} of 10000 chars omitted" in out
