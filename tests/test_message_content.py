# Copyright (C) 2025 Luis González Fernández
# SPDX-License-Identifier: GPL-3.0-or-later

from agents_discussion.graph import _message_content


class _Msg:
    def __init__(self, content: object) -> None:
        self.content = content


def test_string_content_unchanged() -> None:
    assert _message_content(_Msg("hola mundo")) == "hola mundo"


def test_content_block_list_is_flattened() -> None:
    content = [
        {"type": "text", "text": "primera parte "},
        {"type": "text", "text": "segunda parte"},
    ]
    assert _message_content(_Msg(content)) == "primera parte segunda parte"


def test_non_text_blocks_are_ignored() -> None:
    content = [
        {"type": "reasoning", "text": "razonamiento interno"},
        {"type": "text", "text": "respuesta visible"},
    ]
    assert _message_content(_Msg(content)) == "respuesta visible"


def test_plain_string_response_without_content_attr() -> None:
    assert _message_content("texto plano") == "texto plano"


def test_unexpected_type_falls_back_to_str() -> None:
    assert _message_content(_Msg(42)) == "42"
