# Copyright (C) 2025 Luis González Fernández
# SPDX-License-Identifier: GPL-3.0-or-later

"""Append-only JSONL audit trail of every tool invocation made by agents.

One line per tool call, written to <DATA_DIR>/audit.jsonl. Best-effort:
audit failures never break a running debate.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

from agents_discussion.config import get_settings

_LOCK = threading.Lock()
_MAX_RESULT_CHARS = 2_000


def audit_tool_call(
    run_id: str,
    agent: str,
    tool_name: str,
    args: dict,
    result: str,
    error: bool,
    approval: str,
) -> None:
    """Record a tool invocation. `approval` is one of:
    auto | approved | rejected | timeout."""
    try:
        settings = get_settings()
        path = settings.data_dir / "audit.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "run_id": run_id,
            "agent": agent,
            "tool": tool_name,
            "args": args,
            "result": result[:_MAX_RESULT_CHARS],
            "error": error,
            "approval": approval,
        }
        line = json.dumps(entry, ensure_ascii=False, default=str)
        with _LOCK, path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:  # noqa: BLE001 — auditing must never break the debate
        pass
