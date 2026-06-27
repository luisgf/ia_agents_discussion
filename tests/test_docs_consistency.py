# Copyright (C) 2025 Luis González Fernández
# SPDX-License-Identifier: GPL-3.0-or-later

"""Anti-drift: every code-exposed surface must appear in the generated reference docs.

These checks mirror scripts/gen_docs.py. If the code adds or removes a REST route, env var,
tool, or SSE event and docs/reference/ was not regenerated, these tests fail — run
`python scripts/gen_docs.py` and commit the result.
"""

import re
from pathlib import Path

REF = Path(__file__).resolve().parent.parent / "docs" / "reference"
SRC = Path(__file__).resolve().parent.parent / "src" / "agents_discussion"


def _read(name: str) -> str:
    return (REF / name).read_text(encoding="utf-8")


def test_all_routes_documented():
    from agents_discussion.web import app

    doc = _read("api.md")
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not path or not methods:
            continue
        if path != "/" and not path.startswith("/api/"):
            continue  # skip FastAPI's built-in /docs, /redoc, /openapi.json
        assert f"`{path}`" in doc, f"Route {path} missing from docs/reference/api.md — run scripts/gen_docs.py"


def test_all_env_vars_documented():
    from agents_discussion.config import Settings

    doc = _read("configuration.md")
    for info in Settings.model_fields.values():
        if info.alias:
            assert f"`{info.alias}`" in doc, (
                f"Env var {info.alias} missing from configuration.md — run scripts/gen_docs.py"
            )


def test_all_tools_documented():
    from agents_discussion.tools import _ALL_TOOLS

    doc = _read("tools.md")
    for tool in _ALL_TOOLS:
        assert f"`{tool.name}`" in doc, f"Tool {tool.name} missing from tools.md — run scripts/gen_docs.py"


def test_all_events_documented():
    rx = re.compile(r'"type":\s*"([a-z][a-z_]+)"')
    emitted: set[str] = set()
    for fname in ("graph.py", "runtime.py", "web.py"):
        emitted |= set(rx.findall((SRC / fname).read_text(encoding="utf-8")))
    doc = _read("events.md")
    for ev in sorted(emitted):
        assert f"`{ev}`" in doc, f"SSE event '{ev}' missing from events.md — run scripts/gen_docs.py"
