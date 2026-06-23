# Copyright (C) 2025 Luis González Fernández
# SPDX-License-Identifier: GPL-3.0-or-later

import agents_discussion.graph as g
from agents_discussion.runtime import ToolCache


def test_hit_same_round() -> None:
    cache = ToolCache()
    cache.put("run_ssh_command", {"command": "free -m"}, "salida", "diagnostic_agent", "Diagnóstico", 1)
    hit = cache.get("run_ssh_command", {"command": "free -m"}, 1)
    assert hit is not None
    assert hit.result == "salida"
    assert hit.agent_role == "Diagnóstico"


def test_miss_different_round() -> None:
    cache = ToolCache()
    cache.put("run_ssh_command", {"command": "free -m"}, "salida", "a", "A", 1)
    assert cache.get("run_ssh_command", {"command": "free -m"}, 2) is None


def test_miss_after_ttl() -> None:
    cache = ToolCache()
    cache.put("t", {"x": 1}, "r", "a", "A", 1)
    entry = cache._entries[ToolCache.key("t", {"x": 1})]
    entry.ts -= ToolCache.TTL_SECONDS + 1
    assert cache.get("t", {"x": 1}, 1) is None


def test_key_stable_across_arg_order() -> None:
    assert ToolCache.key("t", {"a": 1, "b": 2}) == ToolCache.key("t", {"b": 2, "a": 1})


def test_different_args_different_key() -> None:
    cache = ToolCache()
    cache.put("t", {"command": "df -h"}, "r1", "a", "A", 1)
    assert cache.get("t", {"command": "df -h /var"}, 1) is None


# ── Integración con _run_with_tools (modelo y tools stub, sin LLM) ─────


class _FakeResponse:
    def __init__(self, content: str, tool_calls: list | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls or []
        self.usage_metadata = {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}


class _FakeModel:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = responses
        self._i = 0

    def bind_tools(self, tools: list) -> "_FakeModel":
        return self

    def invoke(self, messages: list) -> _FakeResponse:
        response = self._responses[self._i]
        self._i += 1
        return response


class _FakeTool:
    name = "fake_probe"

    def __init__(self) -> None:
        self.invocations = 0

    def invoke(self, args: dict) -> str:
        self.invocations += 1
        return f"resultado-{self.invocations}"


class _FakeSettings:
    tools_enabled = True
    max_tool_calls_per_agent = 8
    max_consecutive_errors = 3


def test_run_with_tools_serves_duplicate_from_cache(monkeypatch) -> None:
    tool = _FakeTool()
    call = {"name": "fake_probe", "args": {"target": "db"}, "id": "tc1"}
    dup = {"name": "fake_probe", "args": {"target": "db"}, "id": "tc2"}
    model = _FakeModel(
        [
            _FakeResponse("pensando", tool_calls=[call, dup]),
            _FakeResponse("respuesta final"),
        ]
    )
    monkeypatch.setattr(g, "get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(g, "get_tools", lambda: [tool])
    monkeypatch.setattr(g, "audit_tool_call", lambda *a, **k: None)

    content, tool_log, usage = g._run_with_tools(
        lambda: model,
        "diagnostic_agent",
        "sys",
        "user",
        run_id="",
        round_number=1,
    )

    assert content == "respuesta final"
    assert tool.invocations == 1, "la segunda llamada idéntica debe servirse del caché"
    assert len(tool_log) == 2
    assert tool_log[0]["approval"] == "auto"
    assert tool_log[1]["approval"] == "cached"
    assert tool_log[1]["result"].startswith("[cached:")
    assert tool_log[0]["result"] in tool_log[1]["result"]


def test_run_with_tools_does_not_cache_errors(monkeypatch) -> None:
    class _FailingTool:
        name = "fake_probe"
        invocations = 0

        def invoke(self, args: dict) -> str:
            type(self).invocations += 1
            raise RuntimeError("boom")

    tool = _FailingTool()
    call1 = {"name": "fake_probe", "args": {"x": 1}, "id": "a"}
    call2 = {"name": "fake_probe", "args": {"x": 1}, "id": "b"}
    model = _FakeModel(
        [
            _FakeResponse("r1", tool_calls=[call1]),
            _FakeResponse("r2", tool_calls=[call2]),
            _FakeResponse("fin"),
        ]
    )
    monkeypatch.setattr(g, "get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(g, "get_tools", lambda: [tool])
    monkeypatch.setattr(g, "audit_tool_call", lambda *a, **k: None)

    _, tool_log, _ = g._run_with_tools(
        lambda: model,
        "diagnostic_agent",
        "sys",
        "user",
        run_id="",
        round_number=1,
    )

    assert _FailingTool.invocations == 2, "los errores no se cachean: la repetición se re-ejecuta"
    assert all(entry["error"] for entry in tool_log)
