import asyncio
import json
import os
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from agents_discussion.config import get_settings
from agents_discussion.context_files import read_context_file
from agents_discussion.graph import stream_debate_events
from agents_discussion.project_context import build_project_context
from agents_discussion.report import build_markdown_report
from agents_discussion.runtime import (
    RunCancelled,
    RunControl,
    get_control,
    register_control,
    unregister_control,
)
from agents_discussion.state import DebateMessage


load_dotenv()

STATIC_DIR = Path(__file__).parent / "static"


# ── Run persistence ────────────────────────────────────────────────────────────

class RunStore:
    """One JSON file per run.  Stub written on creation, completed on finish."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        self._mark_orphans()

    def _mark_orphans(self) -> None:
        """Runs left as 'running' from a crashed/restarted process → 'interrupted'."""
        for p in self.data_dir.glob("*.json"):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                if d.get("status") == "running":
                    d["status"] = "interrupted"
                    p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass

    def _path(self, run_id: str) -> Path:
        return self.data_dir / f"{run_id}.json"

    def create_stub(self, meta: dict) -> None:
        """Write stub so the run appears in the history list immediately."""
        stub = {**meta, "events": []}
        self._path(meta["run_id"]).write_text(
            json.dumps(stub, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def save(self, run_id: str, data: dict) -> None:
        """Atomically write the complete run record (temp-file + rename)."""
        p = self._path(run_id)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)

    def get(self, run_id: str) -> dict | None:
        p = self._path(run_id)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None

    def list_runs(self) -> list[dict]:
        """Return run metadata (no context/events) sorted newest-first."""
        keys = ("run_id", "topic", "timestamp", "status", "models", "template",
                "language", "parent_run_id")
        runs = []
        for p in self.data_dir.glob("*.json"):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                runs.append({k: d.get(k) for k in keys})
            except Exception:  # noqa: BLE001
                pass
        return sorted(runs, key=lambda r: r.get("timestamp") or "", reverse=True)

    def delete(self, run_id: str) -> bool:
        p = self._path(run_id)
        if p.exists():
            p.unlink()
            return True
        return False


def _init_store() -> RunStore:
    try:
        settings = get_settings()
        path = Path(settings.data_dir)
    except Exception:  # noqa: BLE001
        path = Path.home() / ".local" / "share" / "agents-discussion" / "runs"
    return RunStore(path)


# ── Run sessions (live runs) ──────────────────────────────────────────────────

# Streaming-only events: needed by live SSE subscribers but redundant once the
# run finishes (agent_reasoning/agent_completed/tool_call carry the final data),
# so they are dropped when persisting the run record to disk.
_EPHEMERAL_EVENTS = frozenset({"agent_turn_started", "agent_delta"})


class RunSession:
    """In-memory state of a running debate. The debate executes in a worker
    thread (the graph is synchronous); SSE subscribers poll the event list."""

    def __init__(self, meta: dict, context: str) -> None:
        self.meta = meta  # run_id, topic, timestamp, models, template, language, parent_run_id
        self.context = context
        self.status = "running"
        self.events: list[dict] = []
        self._lock = threading.Lock()
        self.control: RunControl | None = None

    @property
    def run_id(self) -> str:
        return self.meta["run_id"]

    @property
    def finished(self) -> bool:
        return self.status != "running"

    def publish(self, event: dict) -> None:
        with self._lock:
            self.events.append(event)

    def events_from(self, index: int) -> list[dict]:
        with self._lock:
            return list(self.events[index:])

    def record(self) -> dict:
        with self._lock:
            return {
                **self.meta,
                "status": self.status,
                "context": self.context,
                "events": [e for e in self.events if e.get("type") not in _EPHEMERAL_EVENTS],
            }


app = FastAPI(title="Agents Discussion Web")
SESSIONS: dict[str, RunSession] = {}
store = _init_store()

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _run_debate_sync(session: RunSession) -> None:
    """Worker-thread body: drive the debate graph and publish every event."""
    models = session.meta.get("models", {})
    efforts = session.meta.get("reasoning_effort", {})
    try:
        for event in stream_debate_events(
            session.meta["topic"],
            session.context,
            diagnostic_model=models.get("diagnostic", ""),
            skeptic_model=models.get("skeptic", ""),
            moderator_model=models.get("moderator", ""),
            summary_model=models.get("summary", ""),
            run_id=session.run_id,
            template=session.meta.get("template", ""),
            language=session.meta.get("language", ""),
            initial_history=_history_from_events(
                (store.get(session.meta["parent_run_id"]) or {}).get("events", [])
            ) if session.meta.get("parent_run_id") else None,
            diagnostic_reasoning_effort=efforts.get("diagnostic", ""),
            skeptic_reasoning_effort=efforts.get("skeptic", ""),
            moderator_reasoning_effort=efforts.get("moderator", ""),
        ):
            if session.control is not None and session.control.cancelled:
                session.status = "cancelled"
                session.publish({"type": "run_cancelled"})
                return
            session.publish(event)
        session.status = "completed"
    except RunCancelled:
        session.status = "cancelled"
        session.publish({"type": "run_cancelled"})
    except Exception as exc:  # noqa: BLE001
        session.status = "error"
        session.publish({"type": "error", "message": str(exc)})


async def _drive_run(session: RunSession) -> None:
    try:
        await asyncio.to_thread(_run_debate_sync, session)
    finally:
        store.save(session.run_id, session.record())
        unregister_control(session.run_id)
        SESSIONS.pop(session.run_id, None)


def _history_from_events(events: list[dict]) -> list[DebateMessage]:
    """Rebuild the debate history of a finished run from its stored events,
    so a resumed debate starts with the full prior conversation."""
    role_map = {
        "diagnostic_agent": "diagnostic_agent",
        "skeptic_agent": "skeptic_agent",
        "diagnostic_rebuttal_agent": "diagnostic_rebuttal",
    }
    history: list[DebateMessage] = []
    for ev in events or []:
        etype = ev.get("type")
        if etype == "agent_completed" and ev.get("node") in role_map:
            history.append(DebateMessage(role=role_map[ev["node"]], content=str(ev.get("content", ""))))
        elif etype == "moderator_decision" and ev.get("decision"):
            history.append(
                DebateMessage(
                    role="moderator",
                    content=json.dumps(ev["decision"], ensure_ascii=False, indent=2),
                )
            )
        elif etype == "user_comment" and ev.get("content"):
            history.append(DebateMessage(role="user", content=str(ev["content"])))
    return history


def _start_run(meta: dict, context: str, *, pause_between_rounds: bool, require_approval: bool) -> RunSession:
    settings = get_settings()
    session = RunSession(meta, context)
    control = RunControl(
        meta["run_id"],
        session.publish,
        require_approval=require_approval,
        approval_tools=frozenset(settings.approval_tool_set()),
        pause_between_rounds=pause_between_rounds,
        approval_timeout=settings.approval_timeout_seconds,
        comment_timeout=settings.comment_timeout_seconds,
    )
    session.control = control
    register_control(control)
    SESSIONS[meta["run_id"]] = session
    store.create_stub({**meta, "status": "running", "context": context})
    if meta.get("parent_run_id"):
        session.publish({
            "type": "run_resumed",
            "parent_run_id": meta["parent_run_id"],
            "parent_topic": meta.get("parent_topic", ""),
        })
    asyncio.get_running_loop().create_task(_drive_run(session))
    return session


def _parse_optional_bool(value: str, default: bool) -> bool:
    if value == "":
        return default
    return value.strip().lower() in ("1", "true", "on", "yes")


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/settings")
async def settings_api() -> JSONResponse:
    try:
        s = get_settings()
        return JSONResponse({
            "diagnostic_model":      s.diagnostic_model,
            "skeptic_model":         s.skeptic_model,
            "moderator_model":       s.moderator_model,
            "summary_model":         s.summary_model,
            "diagnostic_reasoning_effort": s.diagnostic_reasoning_effort,
            "skeptic_reasoning_effort":    s.skeptic_reasoning_effort,
            "moderator_reasoning_effort":  s.moderator_reasoning_effort,
            "max_rounds":            s.max_rounds,
            "confidence_threshold":  s.confidence_threshold,
            "early_out_threshold":   s.early_out_threshold,
            "prompt_template":       s.prompt_template,
            "prompt_language":       s.prompt_language,
            "tool_approval_required": s.tool_approval_required,
            "compress_history":      s.compress_history,
        })
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/prompts")
async def prompts_api() -> JSONResponse:
    from agents_discussion.prompt_store import list_templates  # noqa: PLC0415

    return JSONResponse({"templates": list_templates()})


# ── Model catalog cache ───────────────────────────────────────────────────────

_models_cache: list[dict] | None = None
_models_cache_ts: float = 0.0
_MODELS_TTL = 300.0  # 5 minutes

_COPILOT_FALLBACK: list[dict] = [
    {"id": "copilot/gpt-4o",             "name": "GPT-4o",             "provider": "copilot"},
    {"id": "copilot/gpt-4.1",            "name": "GPT-4.1",            "provider": "copilot"},
    {"id": "copilot/gpt-4o-mini",        "name": "GPT-4o mini",        "provider": "copilot"},
    {"id": "copilot/claude-sonnet-4.6",  "name": "Claude Sonnet 4.6",  "provider": "copilot"},
    {"id": "copilot/claude-haiku-4.5",   "name": "Claude Haiku 4.5",   "provider": "copilot"},
    {"id": "copilot/gemini-3.5-flash",   "name": "Gemini 3.5 Flash",   "provider": "copilot"},
    {"id": "copilot/gemini-3.1-pro-preview", "name": "Gemini 3.1 Pro Preview", "provider": "copilot"},
]


async def _fetch_models() -> list[dict]:
    settings = get_settings()
    ca_bundle = os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE")
    verify: str | bool = ca_bundle or True
    result: list[dict] = []

    async with httpx.AsyncClient(verify=verify, timeout=10.0) as client:
        # ── GitHub Models ────────────────────────────────────────────
        if settings.github_token:
            try:
                r = await client.get(
                    "https://models.github.ai/v1/models",
                    headers={"Authorization": f"Bearer {settings.github_token}"},
                )
                if r.status_code == 200:
                    for m in r.json().get("data", []):
                        mid = m.get("id", "")
                        if mid:
                            result.append({
                                "id": mid,
                                "name": m.get("display_name") or m.get("name") or mid,
                                "provider": "github_models",
                            })
            except Exception:  # noqa: BLE001
                pass

        # ── GitHub Copilot ───────────────────────────────────────────
        try:
            from agents_discussion.auth_copilot import get_ghu_token, get_session_token  # noqa: PLC0415

            ghu = get_ghu_token()
            if ghu:
                session = await asyncio.to_thread(get_session_token, ghu)
                r = await client.get(
                    "https://api.githubcopilot.com/models",
                    headers={
                        "Authorization": f"Bearer {session}",
                        "User-Agent": "GitHubCopilotChat/0.26.7",
                        "Editor-Version": "vscode/1.99.0",
                        "Editor-Plugin-Version": "copilot-chat/0.26.7",
                        "Accept": "application/json",
                    },
                )
                if r.status_code == 200:
                    items = r.json().get("data") or r.json().get("models") or []
                    copilot_models = []
                    for m in items:
                        mid = m.get("id", "")
                        if not mid:
                            continue
                        # Skip non-chat models (embeddings, internal tools)
                        if any(skip in mid for skip in ("embedding", "trajectory-compaction")):
                            continue
                        full_id = mid if mid.startswith("copilot/") else f"copilot/{mid}"
                        copilot_models.append({
                            "id": full_id,
                            "name": m.get("name") or m.get("display_name") or mid,
                            "provider": "copilot",
                        })
                    result.extend(copilot_models if copilot_models else _COPILOT_FALLBACK)
                else:
                    result.extend(_COPILOT_FALLBACK)
        except Exception:  # noqa: BLE001
            result.extend(_COPILOT_FALLBACK)

    if not result:
        result = [
            {"id": "openai/gpt-4.1", "name": "GPT-4.1 (GitHub Models)", "provider": "github_models"},
            {"id": "openai/gpt-4o",  "name": "GPT-4o (GitHub Models)",  "provider": "github_models"},
            *_COPILOT_FALLBACK,
        ]
    return result


@app.get("/api/models")
async def models_api() -> JSONResponse:
    global _models_cache, _models_cache_ts
    now = time.monotonic()
    if _models_cache is not None and (now - _models_cache_ts) < _MODELS_TTL:
        return JSONResponse({"models": _models_cache})
    models = await _fetch_models()
    _models_cache = models
    _models_cache_ts = now
    return JSONResponse({"models": models})


# ── Run lifecycle ─────────────────────────────────────────────────────────────

@app.get("/api/runs")
async def list_runs_api() -> JSONResponse:
    runs = store.list_runs()
    # Live sessions take precedence over their on-disk stub status.
    for r in runs:
        session = SESSIONS.get(r.get("run_id") or "")
        if session:
            r["status"] = session.status
    return JSONResponse({"runs": runs})


@app.post("/api/runs")
async def create_run(
    topic: Annotated[str, Form()],
    diagnostic_model: Annotated[str, Form()] = "",
    skeptic_model:    Annotated[str, Form()] = "",
    moderator_model:  Annotated[str, Form()] = "",
    diagnostic_reasoning_effort: Annotated[str, Form()] = "",
    skeptic_reasoning_effort:    Annotated[str, Form()] = "",
    moderator_reasoning_effort:  Annotated[str, Form()] = "",
    template:         Annotated[str, Form()] = "",
    language:         Annotated[str, Form()] = "",
    pause_between_rounds: Annotated[str, Form()] = "",
    require_approval:     Annotated[str, Form()] = "",
    project_path: Annotated[str, Form()] = "",
    include_patterns: Annotated[str, Form()] = "",
    max_files: Annotated[int, Form()] = 20,
    max_chars_per_file: Annotated[int, Form()] = 12_000,
    no_redact_context: Annotated[bool, Form()] = False,
    incident_file: Annotated[UploadFile | None, File()] = None,
    base_context: Annotated[list[UploadFile] | None, File()] = None,
    ssh_host: Annotated[str, Form()] = "",
    ssh_user: Annotated[str, Form()] = "",
    ssh_port: Annotated[int, Form()] = 22,
    ssh_key_path: Annotated[str, Form()] = "",
) -> JSONResponse:
    try:
        settings = get_settings()
    except ValidationError as exc:
        return JSONResponse({"detail": f"Invalid configuration: {exc}"}, status_code=400)

    try:
        context = await _build_context(
            incident_file=incident_file,
            base_context_files=base_context or [],
            project_path=project_path.strip(),
            include_patterns=_split_patterns(include_patterns),
            max_files=max_files,
            max_chars_per_file=max_chars_per_file,
            redact_context=not no_redact_context,
        )
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"detail": str(exc)}, status_code=400)

    context = _append_ssh_defaults(context, ssh_host, ssh_user, ssh_port, ssh_key_path)

    meta = {
        "run_id":    uuid.uuid4().hex,
        "topic":     topic,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "models": {
            "diagnostic": diagnostic_model or settings.diagnostic_model,
            "skeptic":    skeptic_model    or settings.skeptic_model,
            "moderator":  moderator_model  or settings.moderator_model,
        },
        "reasoning_effort": {
            "diagnostic": diagnostic_reasoning_effort or settings.diagnostic_reasoning_effort,
            "skeptic":    skeptic_reasoning_effort    or settings.skeptic_reasoning_effort,
            "moderator":  moderator_reasoning_effort  or settings.moderator_reasoning_effort,
        },
        "template":      template or settings.prompt_template,
        "language":      language or settings.prompt_language,
        "parent_run_id": None,
    }
    _start_run(
        meta,
        context,
        pause_between_rounds=_parse_optional_bool(pause_between_rounds, False),
        require_approval=_parse_optional_bool(require_approval, settings.tool_approval_required),
    )
    return JSONResponse({"run_id": meta["run_id"]})


@app.post("/api/runs/{run_id}/resume")
async def resume_run(
    run_id: str,
    new_evidence: Annotated[str, Form()] = "",
    evidence_file: Annotated[list[UploadFile] | None, File()] = None,
    pause_between_rounds: Annotated[str, Form()] = "",
    require_approval:     Annotated[str, Form()] = "",
) -> JSONResponse:
    if run_id in SESSIONS:
        return JSONResponse({"detail": "El debate todavía está en curso."}, status_code=409)
    parent = store.get(run_id)
    if parent is None:
        return JSONResponse({"detail": "Run not found."}, status_code=404)
    if not new_evidence.strip() and not (evidence_file or []):
        return JSONResponse(
            {"detail": "Aporta nueva evidencia (texto o archivos) para reanudar."},
            status_code=400,
        )

    settings = get_settings()
    extra_parts: list[str] = []
    if new_evidence.strip():
        extra_parts.append(new_evidence.strip())
    with tempfile.TemporaryDirectory() as tmp_dir:
        for index, upload in enumerate(evidence_file or []):
            if not upload.filename:
                continue
            saved = await _save_upload(
                upload, Path(tmp_dir) / f"evidence-{index}-{Path(upload.filename).name}"
            )
            extra_parts.append(read_context_file(saved, "Additional Evidence File", True))

    context = parent.get("context", "")
    context = (context + "\n\n" if context else "") + (
        "=== Evidencia adicional aportada al reanudar el debate ===\n"
        + "\n\n".join(extra_parts)
    )

    meta = {
        "run_id":        uuid.uuid4().hex,
        "topic":         parent.get("topic", ""),
        "timestamp":     datetime.now(timezone.utc).isoformat(),
        "models":        parent.get("models") or {},
        "reasoning_effort": parent.get("reasoning_effort") or {},
        "template":      parent.get("template", ""),
        "language":      parent.get("language", ""),
        "parent_run_id": run_id,
        "parent_topic":  parent.get("topic", ""),
    }
    _start_run(
        meta,
        context,
        pause_between_rounds=_parse_optional_bool(pause_between_rounds, False),
        require_approval=_parse_optional_bool(require_approval, settings.tool_approval_required),
    )
    return JSONResponse({"run_id": meta["run_id"]})


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str) -> StreamingResponse:
    session = SESSIONS.get(run_id)
    if session is None:
        # Finished run: replay stored events once and close.
        data = store.get(run_id)
        if data is None:
            return StreamingResponse(
                _single_error_event("Run not found."), media_type="text/event-stream"
            )
        return StreamingResponse(
            _replay_stream(data), media_type="text/event-stream"
        )
    return StreamingResponse(_live_stream(session), media_type="text/event-stream")


async def _live_stream(session: RunSession):
    """Subscribe to a live run: replay buffered events, then poll for new ones.
    The debate itself runs independently in a worker thread — closing this
    stream does NOT stop the run, and multiple subscribers are fine."""
    index = 0
    last_beat = time.monotonic()
    while True:
        batch = session.events_from(index)
        for event in batch:
            yield _sse(event)
        index += len(batch)
        if session.finished and not session.events_from(index):
            break
        if batch:
            last_beat = time.monotonic()
        elif time.monotonic() - last_beat > 15:
            yield ": keep-alive\n\n"
            last_beat = time.monotonic()
        await asyncio.sleep(0.15)


async def _replay_stream(data: dict):
    for event in data.get("events") or []:
        yield _sse(event)


@app.get("/api/runs/{run_id}")
async def get_run_api(run_id: str) -> JSONResponse:
    session = SESSIONS.get(run_id)
    if session:
        return JSONResponse(session.record())
    data = store.get(run_id)
    if data is None:
        return JSONResponse({"detail": "Run not found."}, status_code=404)
    return JSONResponse(data)


@app.get("/api/runs/{run_id}/report")
async def run_report_api(run_id: str) -> Response:
    session = SESSIONS.get(run_id)
    data = session.record() if session else store.get(run_id)
    if data is None:
        return JSONResponse({"detail": "Run not found."}, status_code=404)
    markdown = build_markdown_report(data)
    return Response(
        content=markdown,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="diagnostico-{run_id[:8]}.md"',
        },
    )


class ApprovalBody(BaseModel):
    call_id: str
    approved: bool


@app.post("/api/runs/{run_id}/approval")
async def approve_tool_api(run_id: str, body: ApprovalBody) -> JSONResponse:
    control = get_control(run_id)
    if control is None:
        return JSONResponse({"detail": "Run not active."}, status_code=404)
    if not control.resolve_approval(body.call_id, body.approved):
        return JSONResponse({"detail": "Approval request not pending."}, status_code=404)
    return JSONResponse({"status": "ok"})


class OptionsBody(BaseModel):
    require_approval: bool | None = None
    pause_between_rounds: bool | None = None


@app.post("/api/runs/{run_id}/options")
async def update_run_options_api(run_id: str, body: OptionsBody) -> JSONResponse:
    """Change gating options of a *running* debate. Disabling approval
    auto-approves pending requests; disabling the round pause releases a
    gate that is waiting for a comment."""
    control = get_control(run_id)
    if control is None:
        return JSONResponse({"detail": "Run not active."}, status_code=404)
    if body.require_approval is not None:
        control.set_require_approval(body.require_approval)
    if body.pause_between_rounds is not None:
        control.set_pause_between_rounds(body.pause_between_rounds)
    return JSONResponse({
        "status": "ok",
        "require_approval": control.require_approval,
        "pause_between_rounds": control.pause_between_rounds,
    })


class CommentBody(BaseModel):
    comment: str = ""


@app.post("/api/runs/{run_id}/comment")
async def comment_api(run_id: str, body: CommentBody) -> JSONResponse:
    control = get_control(run_id)
    if control is None:
        return JSONResponse({"detail": "Run not active."}, status_code=404)
    if not control.submit_comment(body.comment):
        return JSONResponse({"detail": "Run is not waiting for input."}, status_code=409)
    return JSONResponse({"status": "ok"})


@app.delete("/api/runs/{run_id}")
async def delete_run_api(run_id: str) -> JSONResponse:
    session = SESSIONS.get(run_id)
    if session:
        # Running — signal cancellation; the worker thread flushes and cleans up.
        if session.control is not None:
            session.control.cancel()
        return JSONResponse({"status": "cancelling"})
    # Finished — delete from disk
    store.delete(run_id)
    return JSONResponse({"status": "deleted"})


# ── Internal helpers ──────────────────────────────────────────────────────────

async def _build_context(
    incident_file: UploadFile | None,
    base_context_files: list[UploadFile],
    project_path: str,
    include_patterns: list[str],
    max_files: int,
    max_chars_per_file: int,
    redact_context: bool,
) -> str:
    if max_files < 1:
        raise ValueError("max_files must be greater than 0.")
    if max_chars_per_file < 1:
        raise ValueError("max_chars_per_file must be greater than 0.")

    context_parts: list[str] = []
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_root = Path(tmp_dir)

        for index, upload in enumerate(base_context_files):
            if not upload.filename:
                continue
            saved = await _save_upload(upload, tmp_root / f"base-context-{index}-{Path(upload.filename).name}")
            context_parts.append(read_context_file(saved, "Base Technical Context", redact_context))

        if incident_file and incident_file.filename:
            saved = await _save_upload(incident_file, tmp_root / f"incident-{Path(incident_file.filename).name}")
            context_parts.append(read_context_file(saved, "Incident Context File", redact_context))

    if project_path:
        context_parts.append(
            build_project_context(
                project_path=Path(project_path),
                include_patterns=include_patterns or None,
                max_files=max_files,
                max_chars_per_file=max_chars_per_file,
            )
        )

    return "\n\n".join(context_parts)


def _append_ssh_defaults(context: str, host: str, user: str, port: int, key_path: str) -> str:
    """Inject SSH defaults into context so the LLM uses them as tool-call arguments."""
    lines = []
    if host.strip():
        lines.append(f"Default SSH host: {host.strip()}")
    if user.strip():
        lines.append(f"Default SSH user: {user.strip()}")
    if port and port != 22:
        lines.append(f"Default SSH port: {port}")
    if key_path.strip():
        lines.append(f"Default SSH key path: {key_path.strip()}")
    if not lines:
        return context
    block = "=== SSH Connection Defaults ===\n" + "\n".join(lines)
    return f"{context}\n\n{block}" if context else block


async def _save_upload(upload: UploadFile, destination: Path) -> Path:
    content = await upload.read()
    destination.write_bytes(content)
    return destination


def _split_patterns(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


async def _single_error_event(message: str):
    yield _sse({"type": "error", "message": message})


def _sse(payload: dict[str, object]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def main() -> None:
    try:
        settings = get_settings()
        host, port = settings.web_host, settings.web_port
    except Exception:  # noqa: BLE001
        host, port = "127.0.0.1", 8000
    uvicorn.run("agents_discussion.web:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
