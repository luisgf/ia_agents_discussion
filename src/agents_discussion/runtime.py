"""Thread-safe control channel between the web layer and a running debate.

The debate graph executes synchronously inside a worker thread, while the
web layer lives in the asyncio event loop. A RunControl instance bridges
both worlds with plain threading primitives:

  - cancellation        : DELETE /api/runs/{id} → cancel()
  - tool approval       : graph blocks in request_approval(); the UI resolves
                          it via POST /api/runs/{id}/approval
  - round comments      : graph blocks in wait_for_comment() (human-in-the-loop);
                          the UI resolves it via POST /api/runs/{id}/comment

Controls are looked up by run_id through the module-level registry so graph
nodes only need the run_id present in the debate state. CLI runs have no
registered control and therefore skip gating entirely.
"""
from __future__ import annotations

import threading
import uuid
from typing import Callable


class RunCancelled(Exception):
    """Raised inside the debate when the operator cancels the run."""


class _ApprovalRequest:
    def __init__(self, call_id: str, tool_name: str, args: dict, agent_role: str) -> None:
        self.call_id = call_id
        self.tool_name = tool_name
        self.args = args
        self.agent_role = agent_role
        self.decision: bool | None = None
        self.event = threading.Event()


class RunControl:
    def __init__(
        self,
        run_id: str,
        emit: Callable[[dict], None],
        *,
        require_approval: bool = False,
        approval_tools: frozenset[str] = frozenset(),
        pause_between_rounds: bool = False,
        approval_timeout: int = 300,
        comment_timeout: int = 600,
    ) -> None:
        self.run_id = run_id
        self.emit = emit  # must be thread-safe (web layer appends under lock)
        self.require_approval = require_approval
        self.approval_tools = approval_tools
        self.pause_between_rounds = pause_between_rounds
        self.approval_timeout = approval_timeout
        self.comment_timeout = comment_timeout

        self._cancelled = threading.Event()
        self._approvals: dict[str, _ApprovalRequest] = {}
        self._approvals_lock = threading.Lock()
        self._comment_event = threading.Event()
        self._comment_text: str | None = None
        self._comment_waiting = False

    # ── Cancellation ─────────────────────────────────────────────────────

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def cancel(self) -> None:
        self._cancelled.set()
        # Wake any blocked waits so the worker thread can exit promptly.
        with self._approvals_lock:
            for req in self._approvals.values():
                req.event.set()
        self._comment_event.set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise RunCancelled

    # ── Tool approval (graph side) ───────────────────────────────────────

    def needs_approval(self, tool_name: str) -> bool:
        return self.require_approval and tool_name in self.approval_tools

    def request_approval(self, tool_name: str, args: dict, agent_role: str) -> tuple[bool, str]:
        """Block until the operator approves/rejects, the timeout expires,
        or the run is cancelled. Returns (approved, resolution) where
        resolution is one of: approved | rejected | timeout."""
        call_id = uuid.uuid4().hex[:12]
        req = _ApprovalRequest(call_id, tool_name, args, agent_role)
        with self._approvals_lock:
            self._approvals[call_id] = req

        self.emit({
            "type": "tool_approval_request",
            "call_id": call_id,
            "tool_name": tool_name,
            "args": args,
            "agent_role": agent_role,
        })

        answered = req.event.wait(self.approval_timeout)
        with self._approvals_lock:
            self._approvals.pop(call_id, None)
        self.raise_if_cancelled()

        if not answered:
            resolution = "timeout"
            approved = False
        else:
            approved = bool(req.decision)
            resolution = "approved" if approved else "rejected"

        self.emit({
            "type": "tool_approval_resolved",
            "call_id": call_id,
            "approved": approved,
            "resolution": resolution,
        })
        return approved, resolution

    # ── Live option changes (web side) ───────────────────────────────────

    def set_require_approval(self, value: bool) -> None:
        """Toggle approval gating mid-run. Disabling it auto-approves any
        request currently waiting for the operator."""
        self.require_approval = value
        if not value:
            with self._approvals_lock:
                for req in self._approvals.values():
                    if req.decision is None:
                        req.decision = True
                        req.event.set()

    def set_pause_between_rounds(self, value: bool) -> None:
        """Toggle the human-in-the-loop pause mid-run. Disabling it releases
        a gate currently waiting for a comment."""
        self.pause_between_rounds = value
        if not value and self._comment_waiting:
            self.submit_comment("")

    # ── Tool approval (web side) ─────────────────────────────────────────

    def resolve_approval(self, call_id: str, approved: bool) -> bool:
        with self._approvals_lock:
            req = self._approvals.get(call_id)
            if req is None:
                return False
            req.decision = approved
            req.event.set()
            return True

    def pending_approvals(self) -> list[dict]:
        with self._approvals_lock:
            return [
                {
                    "call_id": r.call_id,
                    "tool_name": r.tool_name,
                    "args": r.args,
                    "agent_role": r.agent_role,
                }
                for r in self._approvals.values()
            ]

    # ── Human-in-the-loop comment (graph side) ───────────────────────────

    def wait_for_comment(self, round_number: int) -> str | None:
        """Block until the operator submits a comment (possibly empty,
        meaning 'continue'), the timeout expires, or the run is cancelled.
        Returns the comment text or None."""
        self._comment_event.clear()
        self._comment_text = None
        self._comment_waiting = True

        self.emit({"type": "awaiting_user_input", "round": round_number})
        answered = self._comment_event.wait(self.comment_timeout)
        self._comment_waiting = False
        self.raise_if_cancelled()

        text = (self._comment_text or "").strip() if answered else ""
        self.emit({"type": "user_comment", "content": text, "round": round_number})
        return text or None

    # ── Human-in-the-loop comment (web side) ─────────────────────────────

    def submit_comment(self, text: str) -> bool:
        if not self._comment_waiting:
            return False
        self._comment_text = text
        self._comment_event.set()
        return True

    @property
    def awaiting_comment(self) -> bool:
        return self._comment_waiting


# ── Registry ─────────────────────────────────────────────────────────────

_REGISTRY: dict[str, RunControl] = {}
_REGISTRY_LOCK = threading.Lock()


def register_control(control: RunControl) -> None:
    with _REGISTRY_LOCK:
        _REGISTRY[control.run_id] = control


def get_control(run_id: str) -> RunControl | None:
    if not run_id:
        return None
    with _REGISTRY_LOCK:
        return _REGISTRY.get(run_id)


def unregister_control(run_id: str) -> None:
    with _REGISTRY_LOCK:
        _REGISTRY.pop(run_id, None)
