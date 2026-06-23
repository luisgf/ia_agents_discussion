# Copyright (C) 2025 Luis González Fernández
# SPDX-License-Identifier: GPL-3.0-or-later

import agents_discussion.graph as g


class _FakeResponse:
    def __init__(self, content: str, tool_calls: list | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls or []
        self.usage_metadata = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}


class _FakeModel:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = responses
        self.invocations = 0

    def bind_tools(self, tools: list) -> "_FakeModel":
        return self

    def invoke(self, messages: list) -> _FakeResponse:
        response = self._responses[self.invocations]
        self.invocations += 1
        return response


class _FakeTool:
    name = "fake_probe"

    def __init__(self) -> None:
        self.invocations = 0

    def invoke(self, args: dict) -> str:
        self.invocations += 1
        return "tool-output"


class _FakeSettings:
    tools_enabled = True
    max_tool_calls_per_agent = 8
    max_consecutive_errors = 3


def _patch(monkeypatch, tool: _FakeTool) -> None:
    monkeypatch.setattr(g, "get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(g, "get_tools", lambda: [tool])
    monkeypatch.setattr(g, "audit_tool_call", lambda *a, **k: None)


_TOOL_CALL = {"name": "fake_probe", "args": {"x": 1}, "id": "tc1"}


def test_empty_final_recovered_by_nudge(monkeypatch) -> None:
    tool = _FakeTool()
    model = _FakeModel(
        [
            _FakeResponse("", tool_calls=[_TOOL_CALL]),
            _FakeResponse(""),  # empty final → nudge
            _FakeResponse("FINAL REPORT"),  # response to the nudge
        ]
    )
    _patch(monkeypatch, tool)

    content, tool_log, usage = g._run_with_tools(
        lambda: model,
        "diagnostic_agent",
        "sys",
        "user",
        run_id="",
        round_number=1,
    )

    assert content == "FINAL REPORT"
    assert model.invocations == 3
    assert tool.invocations == 1
    assert len(tool_log) == 1


def test_empty_after_nudge_yields_placeholder(monkeypatch) -> None:
    tool = _FakeTool()
    model = _FakeModel(
        [
            _FakeResponse("", tool_calls=[_TOOL_CALL]),
            _FakeResponse(""),
            _FakeResponse("   "),  # also empty after the nudge
        ]
    )
    _patch(monkeypatch, tool)

    content, _, _ = g._run_with_tools(
        lambda: model,
        "diagnostic_agent",
        "sys",
        "user",
        run_id="",
        round_number=1,
    )

    assert content == "(The agent returned no final response after 1 tool calls.)"


def test_normal_final_no_extra_invocation(monkeypatch) -> None:
    tool = _FakeTool()
    model = _FakeModel(
        [
            _FakeResponse("", tool_calls=[_TOOL_CALL]),
            _FakeResponse("normal response"),
        ]
    )
    _patch(monkeypatch, tool)

    content, _, _ = g._run_with_tools(
        lambda: model,
        "diagnostic_agent",
        "sys",
        "user",
        run_id="",
        round_number=1,
    )

    assert content == "normal response"
    assert model.invocations == 2


def test_nudge_usage_accumulated(monkeypatch) -> None:
    tool = _FakeTool()
    model = _FakeModel(
        [
            _FakeResponse("", tool_calls=[_TOOL_CALL]),
            _FakeResponse(""),
            _FakeResponse("REPORT"),
        ]
    )
    _patch(monkeypatch, tool)

    _, _, usage = g._run_with_tools(
        lambda: model,
        "diagnostic_agent",
        "sys",
        "user",
        run_id="",
        round_number=1,
    )

    # 3 LLM calls × 15 tokens from the stub
    assert usage["total_tokens"] == 45
    assert usage["input_tokens"] == 30
