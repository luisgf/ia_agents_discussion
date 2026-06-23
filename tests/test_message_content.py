# Copyright (C) 2025 Luis González Fernández
# SPDX-License-Identifier: GPL-3.0-or-later

from agents_discussion.graph import _message_content


class _Msg:
    def __init__(self, content: object) -> None:
        self.content = content


def test_string_content_unchanged() -> None:
    assert _message_content(_Msg("hello world")) == "hello world"


def test_content_block_list_is_flattened() -> None:
    content = [
        {"type": "text", "text": "first part "},
        {"type": "text", "text": "second part"},
    ]
    assert _message_content(_Msg(content)) == "first part second part"


def test_non_text_blocks_are_ignored() -> None:
    content = [
        {"type": "reasoning", "text": "internal reasoning"},
        {"type": "text", "text": "visible response"},
    ]
    assert _message_content(_Msg(content)) == "visible response"


def test_plain_string_response_without_content_attr() -> None:
    assert _message_content("plain text") == "plain text"


def test_unexpected_type_falls_back_to_str() -> None:
    assert _message_content(_Msg(42)) == "42"
