import asyncio
import json
import os
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import ValidationError

from agents_discussion.config import get_settings
from agents_discussion.context_files import read_context_file
from agents_discussion.graph import stream_debate_events
from agents_discussion.project_context import build_project_context


load_dotenv()


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

    def create(self, run_id: str, topic: str, timestamp: str, models: dict) -> None:
        """Write stub so the run appears in the history list immediately."""
        stub = {
            "run_id": run_id,
            "topic": topic,
            "timestamp": timestamp,
            "status": "running",
            "models": models,
            "context": "",
            "events": [],
        }
        self._path(run_id).write_text(
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
        runs = []
        for p in self.data_dir.glob("*.json"):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                runs.append(
                    {k: d.get(k) for k in ("run_id", "topic", "timestamp", "status", "models")}
                )
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


# ── App + module state ────────────────────────────────────────────────────────

app = FastAPI(title="Agents Discussion Web")
RUNS: dict[str, dict] = {}
store = _init_store()

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


# ── HTML ──────────────────────────────────────────────────────────────────────

HTML = """
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Agents Discussion</title>
  <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
  <style>
    /* ── Reset & tokens ─────────────────────────────────────────── */
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    :root {
      --bg:       #07090f;
      --s1:       #0d1424;
      --s2:       #111c30;
      --s3:       #18253c;
      --border:   #1c2d44;
      --text:     #dde6f5;
      --dim:      #7a90ad;
      --muted:    #3d5470;

      --diag:     #3b82f6;
      --skeptic:  #f43f5e;
      --rebuttal: #f59e0b;
      --mod:      #a78bfa;
      --final:    #10b981;
      --err:      #f43f5e;

      --r:   12px;
      --r-s:  8px;
      color-scheme: dark;
    }

    html, body { height: 100%; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
      background: var(--bg);
      color: var(--text);
      font-size: 14px;
      line-height: 1.6;
      overflow: hidden;
    }

    /* ── App shell ──────────────────────────────────────────────── */
    .app {
      display: grid;
      grid-template-columns: 320px 1fr;
      height: 100vh;
    }

    /* ── Sidebar ────────────────────────────────────────────────── */
    .sidebar {
      background: var(--s1);
      border-right: 1px solid var(--border);
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }

    .sidebar-head {
      padding: 18px 20px 14px;
      border-bottom: 1px solid var(--border);
      flex-shrink: 0;
    }

    .brand { display: flex; align-items: center; gap: 10px; }

    .brand-icon {
      width: 34px; height: 34px;
      background: linear-gradient(135deg, #2563eb, #7c3aed);
      border-radius: 9px;
      display: flex; align-items: center; justify-content: center;
      color: #fff; flex-shrink: 0;
    }

    .brand-name { font-size: 15px; font-weight: 700; letter-spacing: -.3px; }
    .brand-sub  { font-size: 11px; color: var(--dim); margin-top: 1px; }

    .status-pill {
      display: inline-flex; align-items: center; gap: 6px;
      font-size: 11px; font-weight: 600;
      padding: 3px 9px; border-radius: 99px; margin-top: 10px;
    }
    .status-pill .dot { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; }

    .status-pill.idle       { background: rgba(100,116,139,.1); color: var(--dim); }
    .status-pill.idle .dot  { background: var(--dim); }
    .status-pill.preparing,
    .status-pill.running    { background: rgba(59,130,246,.12); color: #7db5f8; }
    .status-pill.preparing .dot,
    .status-pill.running .dot { background: var(--diag); animation: blink 1s infinite; }
    .status-pill.done       { background: rgba(16,185,129,.12); color: #34d399; }
    .status-pill.done .dot  { background: var(--final); }
    .status-pill.error      { background: rgba(244,63,94,.12); color: #fb7185; }
    .status-pill.error .dot { background: var(--err); }

    @keyframes blink { 0%,100%{opacity:1} 50%{opacity:.3} }

    /* ── Form ───────────────────────────────────────────────────── */
    .sidebar-body { flex: 1; overflow-y: auto; padding: 16px 20px 24px; }

    ::-webkit-scrollbar { width: 4px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: var(--muted); border-radius: 4px; }

    .fsect-title {
      font-size: 10px; font-weight: 700;
      text-transform: uppercase; letter-spacing: .9px;
      color: var(--muted); margin-bottom: 10px;
    }

    .field { margin-bottom: 11px; }
    .field label { display: block; font-size: 12px; font-weight: 500; color: #8aaccc; margin-bottom: 4px; }
    .field-hint  { font-size: 11px; color: var(--muted); margin-top: 3px; line-height: 1.4; }

    textarea, input[type="text"], input[type="number"], select {
      width: 100%;
      background: var(--s2); border: 1px solid var(--border);
      border-radius: var(--r-s); color: var(--text);
      font: inherit; padding: 8px 11px;
      transition: border-color .15s; outline: none;
    }
    textarea { min-height: 84px; resize: vertical; }
    textarea:focus, input:focus, select:focus {
      border-color: var(--diag);
      box-shadow: 0 0 0 3px rgba(59,130,246,.1);
    }
    select { cursor: pointer; appearance: none;
      background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%237a90ad' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpolyline points='6 9 12 15 18 9'/%3E%3C/svg%3E");
      background-repeat: no-repeat; background-position: right 10px center;
      padding-right: 30px;
    }
    select option { background: var(--s2); }
    select:disabled { opacity: .5; cursor: not-allowed; }
    input[type="file"] {
      width: 100%;
      background: var(--s2); border: 1px dashed var(--border);
      border-radius: var(--r-s); color: var(--dim);
      font: inherit; padding: 7px 11px; cursor: pointer;
    }
    input[type="file"]:hover { border-color: var(--diag); }

    .checkbox-row {
      display: flex; align-items: center; gap: 7px;
      font-size: 12px; color: #8aaccc; cursor: pointer;
    }
    .checkbox-row input[type="checkbox"] { width: auto; accent-color: var(--diag); }

    details.adv > summary {
      list-style: none; cursor: pointer;
      font-size: 12px; color: var(--dim);
      display: flex; align-items: center; gap: 5px;
      padding: 6px 0; user-select: none;
      transition: color .15s;
    }
    details.adv > summary:hover { color: var(--text); }
    details.adv > summary::before { content: '▸'; transition: transform .2s; }
    details.adv[open] > summary::before { transform: rotate(90deg); }
    details.adv > summary::-webkit-details-marker { display: none; }
    .adv-body { padding-top: 10px; display: flex; flex-direction: column; gap: 11px; }
    .two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }

    /* ── Buttons ────────────────────────────────────────────────── */
    .btn-run {
      width: 100%; margin-top: 18px;
      background: linear-gradient(135deg, #1d4ed8, #6d28d9);
      color: #fff; font: 600 13px inherit;
      border: none; border-radius: var(--r-s);
      padding: 11px 16px; cursor: pointer;
      transition: opacity .2s, transform .1s;
      letter-spacing: .1px;
    }
    .btn-run:hover:not(:disabled) { opacity: .88; transform: translateY(-1px); }
    .btn-run:active:not(:disabled) { transform: none; }
    .btn-run:disabled { opacity: .4; cursor: not-allowed; }

    .btn-stop {
      width: 100%; margin-top: 8px;
      background: rgba(244,63,94,.08);
      color: #fb7185; font: 600 13px inherit;
      border: 1px solid rgba(244,63,94,.22); border-radius: var(--r-s);
      padding: 9px 16px; cursor: pointer;
      transition: background .15s, border-color .15s;
    }
    .btn-stop:hover { background: rgba(244,63,94,.16); border-color: rgba(244,63,94,.4); }
    .btn-stop.hidden { display: none !important; }

    /* ── Convo panel ────────────────────────────────────────────── */
    .convo {
      display: flex; flex-direction: column;
      overflow: hidden; background: var(--bg);
    }

    /* ── Tab strip ──────────────────────────────────────────────── */
    .convo-tabs {
      display: flex; border-bottom: 1px solid var(--border);
      background: var(--s1); flex-shrink: 0; padding: 0 22px;
    }
    .tab-btn {
      background: none; border: none; color: var(--dim);
      font: 600 11px inherit; padding: 12px 14px 11px;
      cursor: pointer; border-bottom: 2px solid transparent;
      transition: color .15s, border-color .15s;
      text-transform: uppercase; letter-spacing: .6px;
    }
    .tab-btn:hover { color: var(--text); }
    .tab-btn.active { color: var(--diag); border-bottom-color: var(--diag); }

    /* ── Thread ─────────────────────────────────────────────────── */
    #thread {
      flex: 1; overflow-y: auto;
      padding: 22px 28px 36px;
      display: flex; flex-direction: column;
    }

    /* ── Empty state ────────────────────────────────────────────── */
    .empty-state {
      flex: 1; display: flex; flex-direction: column;
      align-items: center; justify-content: center;
      gap: 10px; color: var(--muted); text-align: center; padding: 48px;
    }
    .empty-state svg { opacity: .25; }
    .empty-state h2 { font-size: 15px; font-weight: 600; color: var(--dim); margin-top: 4px; }
    .empty-state p  { font-size: 12px; max-width: 240px; line-height: 1.5; }
    .empty-state.hidden { display: none; }

    /* ── Run topic header ───────────────────────────────────────── */
    .run-header {
      background: linear-gradient(135deg,rgba(37,99,235,.08),rgba(109,40,217,.08));
      border: 1px solid rgba(59,130,246,.18);
      border-radius: var(--r); padding: 13px 16px; margin-bottom: 18px;
      animation: fadeUp .3s ease-out; flex-shrink: 0;
    }
    .run-header-label {
      font-size: 10px; font-weight: 700;
      text-transform: uppercase; letter-spacing: .9px;
      color: var(--diag); margin-bottom: 4px;
    }
    .run-header-topic { font-size: 14px; font-weight: 600; line-height: 1.4; }
    .run-header-meta  { font-size: 11px; color: var(--dim); margin-top: 3px; }

    /* ── Round divider ──────────────────────────────────────────── */
    .round-sep {
      display: flex; align-items: center; gap: 10px;
      margin: 18px 0 14px; animation: fadeUp .25s ease-out;
      flex-shrink: 0;
    }
    .round-sep::before, .round-sep::after {
      content: ''; flex: 1; height: 1px; background: var(--border);
    }
    .round-sep span {
      font-size: 10px; font-weight: 700;
      text-transform: uppercase; letter-spacing: .8px;
      color: var(--muted); white-space: nowrap;
    }
    .round-sep em { font-style: normal; color: var(--border); }

    /* ── Agent cards ────────────────────────────────────────────── */
    .acard {
      background: var(--s1); border: 1px solid var(--border);
      border-radius: var(--r); overflow: hidden;
      margin-bottom: 10px; animation: fadeUp .3s ease-out;
      flex-shrink: 0;
    }
    .acard-head {
      display: flex; align-items: center; justify-content: space-between;
      padding: 11px 14px; border-bottom: 1px solid var(--border);
      gap: 10px; cursor: pointer; user-select: none;
    }
    .acard-chevron {
      color: var(--dim); flex-shrink: 0;
      transition: transform .2s ease;
    }
    .acard.collapsed .acard-chevron { transform: rotate(-90deg); }
    .acard.collapsed .card-body,
    .acard.collapsed .mod-body,
    .acard.collapsed .conf-row { display: none; }
    .agent-id { display: flex; align-items: center; gap: 9px; }
    .agent-ico {
      width: 32px; height: 32px; border-radius: 8px;
      display: flex; align-items: center; justify-content: center;
      flex-shrink: 0;
    }
    .agent-nm  { font-size: 13px; font-weight: 600; display: block; }
    .agent-sub { font-size: 11px; color: var(--dim); display: block; }

    /* Per-agent accent */
    .a-diag     { border-left: 3px solid var(--diag); }
    .a-diag .agent-ico { background: rgba(59,130,246,.12); color: var(--diag); }
    .a-skeptic  { border-left: 3px solid var(--skeptic); }
    .a-skeptic .agent-ico { background: rgba(244,63,94,.12); color: var(--skeptic); }
    .a-rebuttal { border-left: 3px solid var(--rebuttal); }
    .a-rebuttal .agent-ico { background: rgba(245,158,11,.12); color: var(--rebuttal); }
    .a-mod      { border-left: 3px solid var(--mod); }
    .a-mod .agent-ico { background: rgba(167,139,250,.12); color: var(--mod); }
    .a-final    {
      border-left: 3px solid var(--final);
      background: linear-gradient(135deg,rgba(16,185,129,.04),var(--s1));
    }
    .a-final .agent-ico { background: rgba(16,185,129,.12); color: var(--final); }
    .a-err      { border-left: 3px solid var(--err); }
    .a-err .agent-ico   { background: rgba(244,63,94,.12); color: var(--err); }
    .a-info     { border-left: 3px solid var(--muted); }
    .a-info .agent-ico  { background: rgba(61,84,112,.25); color: var(--dim); }

    /* Tool call cards */
    .tc-card {
      border: 1px solid var(--border); border-left: 3px solid #6366f1;
      border-radius: var(--r-s); margin: 4px 0; font-size: 12.5px;
      background: rgba(99,102,241,.05);
    }
    .tc-card.tc-err { border-left-color: var(--err); background: rgba(244,63,94,.05); }
    .tc-head {
      display: flex; align-items: center; gap: 8px;
      padding: 7px 12px; cursor: pointer; user-select: none;
    }
    .tc-ico  { font-size: 13px; }
    .tc-name { font-weight: 600; color: #a5b4fc; flex: 1; }
    .tc-card.tc-err .tc-name { color: #fca5a5; }
    .tc-arrow { color: #64748b; transition: transform .2s; font-size: 11px; }
    .tc-card.open .tc-arrow { transform: rotate(90deg); }
    .tc-body { display: none; padding: 6px 12px 10px; border-top: 1px solid var(--border); }
    .tc-card.open .tc-body { display: block; }
    .tc-section-lbl { font-size: 11px; text-transform: uppercase; letter-spacing: .05em;
                       color: #64748b; margin: 6px 0 3px; }
    .tc-pre {
      background: rgba(0,0,0,.35); border: 1px solid var(--border);
      border-radius: 4px; padding: 7px 10px; font-family: ui-monospace, Consolas, monospace;
      font-size: 11.5px; color: #c4d4ea; overflow-x: auto; white-space: pre-wrap;
      word-break: break-all; max-height: 220px; overflow-y: auto; margin: 0;
    }
    .tc-wrapper { margin: 6px 15px 0; flex-shrink: 0; }

    /* Card prose body */
    .card-body {
      padding: 13px 15px; font-size: 13.5px;
      line-height: 1.68; color: #c4d4ea;
    }
    .card-body > *:first-child { margin-top: 0 !important; }
    .card-body > *:last-child  { margin-bottom: 0 !important; }
    .card-body h1,.card-body h2,.card-body h3,.card-body h4 {
      color: var(--text); margin: 14px 0 5px; line-height: 1.3;
    }
    .card-body h1 { font-size: 16px; }
    .card-body h2 { font-size: 14.5px; }
    .card-body h3 { font-size: 13.5px; }
    .card-body p  { margin: 0 0 8px; }
    .card-body p:last-child { margin-bottom: 0; }
    .card-body ul,.card-body ol { padding-left: 20px; margin: 5px 0 9px; }
    .card-body li { margin: 3px 0; }
    .card-body strong { color: var(--text); font-weight: 600; }
    .card-body em     { color: #96aec8; }
    .card-body code {
      background: rgba(255,255,255,.07); border: 1px solid rgba(255,255,255,.08);
      border-radius: 4px; padding: 1px 5px;
      font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
      font-size: 12px; color: #93c5fd;
    }
    .card-body pre {
      background: rgba(0,0,0,.35); border: 1px solid var(--border);
      border-radius: var(--r-s); padding: 11px 13px; overflow-x: auto; margin: 8px 0;
    }
    .card-body pre code { background: none; border: none; padding: 0; color: #c4d4ea; }
    .card-body blockquote {
      border-left: 3px solid var(--border); padding-left: 11px;
      color: var(--dim); margin: 7px 0;
    }

    /* ── Moderator decision ─────────────────────────────────────── */
    .conf-row {
      display: flex; align-items: center; gap: 10px;
      padding: 9px 14px; background: var(--s2);
      border-bottom: 1px solid var(--border);
    }
    .conf-lbl {
      font-size: 10px; font-weight: 700;
      text-transform: uppercase; letter-spacing: .7px;
      color: var(--muted); white-space: nowrap;
    }
    .conf-track {
      flex: 1; height: 5px; background: var(--s3);
      border-radius: 99px; overflow: hidden;
    }
    .conf-fill { height: 100%; border-radius: 99px; transition: width .6s ease; }
    .conf-val { font-size: 13px; font-weight: 700; min-width: 36px; text-align: right; }

    .mod-body { padding: 13px 15px; display: flex; flex-direction: column; gap: 13px; }
    .mod-row2 { display: grid; grid-template-columns: 1fr 1fr; gap: 13px; }
    .msect-title {
      font-size: 10px; font-weight: 700;
      text-transform: uppercase; letter-spacing: .8px;
      color: var(--muted); margin-bottom: 5px;
    }
    .msect-body { font-size: 13px; color: #c4d4ea; line-height: 1.55; }
    .msect-body.it { color: var(--dim); font-style: italic; }
    .mlist { list-style: none; display: flex; flex-direction: column; gap: 4px; }
    .mlist li { display: flex; gap: 7px; font-size: 12.5px; color: #b0c4de; line-height: 1.5; }
    .mlist li::before { content: '·'; color: var(--muted); flex-shrink: 0; }
    .fix-box {
      background: rgba(59,130,246,.06); border: 1px solid rgba(59,130,246,.18);
      border-radius: var(--r-s); padding: 10px 12px;
    }

    /* Badges */
    .dbadge {
      font-size: 10px; font-weight: 700; padding: 3px 8px;
      border-radius: 99px; text-transform: uppercase; letter-spacing: .5px; white-space: nowrap;
    }
    .db-continue             { background: rgba(245,158,11,.14);  color: #fbbf24; }
    .db-final_diagnosis      { background: rgba(16,185,129,.14);  color: #34d399; }
    .db-needs_more_data      { background: rgba(59,130,246,.14);  color: #7dd3fc; }
    .db-propose_fix          { background: rgba(244,63,94,.14);   color: #fb7185; }
    .db-structured_uncertainty { background: rgba(107,114,128,.14); color: #9ca3af; }

    .rbadge {
      display: inline-flex; font-size: 10px; font-weight: 700;
      padding: 2px 7px; border-radius: 99px;
      text-transform: uppercase; letter-spacing: .4px;
    }
    .rc  { background: rgba(239,68,68,.18); color: #f87171; }
    .rh  { background: rgba(249,115,22,.18); color: #fb923c; }
    .rm  { background: rgba(234,179,8,.18);  color: #fbbf24; }
    .rl  { background: rgba(34,197,94,.18);  color: #4ade80; }
    .ru  { background: rgba(107,114,128,.18);color: #9ca3af; }

    /* ── Final card ─────────────────────────────────────────────── */
    .final-stats {
      display: grid; grid-template-columns: repeat(3, 1fr);
      border-bottom: 1px solid var(--border);
    }
    .fstat {
      padding: 10px 14px; display: flex; flex-direction: column; gap: 2px;
      border-right: 1px solid var(--border);
    }
    .fstat:last-child { border-right: none; }
    .fstat-lbl { font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: .7px; color: var(--muted); }
    .fstat-val { font-size: 15px; font-weight: 700; color: var(--text); }

    /* ── Typing indicator ───────────────────────────────────────── */
    .typing {
      display: flex; align-items: center; gap: 9px;
      padding: 9px 14px; margin-bottom: 10px;
    }
    .typing.hidden { display: none; }
    .typing-dots { display: flex; gap: 4px; align-items: center; }
    .typing-dots span {
      width: 6px; height: 6px; border-radius: 50%;
      background: var(--muted); animation: tdot 1.4s infinite ease-in-out;
    }
    .typing-dots span:nth-child(2) { animation-delay: .2s; }
    .typing-dots span:nth-child(3) { animation-delay: .4s; }
    @keyframes tdot { 0%,60%,100%{transform:translateY(0);opacity:.35} 30%{transform:translateY(-5px);opacity:1} }
    .typing-lbl { font-size: 12px; color: var(--dim); font-style: italic; }

    /* ── Replay banner ──────────────────────────────────────────── */
    .replay-banner {
      background: rgba(167,139,250,.08); border: 1px solid rgba(167,139,250,.18);
      border-radius: var(--r); padding: 10px 14px; margin-bottom: 14px;
      display: flex; align-items: center; gap: 8px;
      font-size: 12px; color: #c4b5fd; flex-shrink: 0; flex-wrap: wrap;
    }
    .replay-banner strong { color: #ddd6fe; }

    /* ── History panel ──────────────────────────────────────────── */
    #hist-panel {
      flex: 1; overflow-y: auto;
      padding: 22px 28px 36px;
      display: none; flex-direction: column;
    }
    #hist-panel.active { display: flex; }

    .hist-title {
      font-size: 15px; font-weight: 700; margin-bottom: 18px;
      color: var(--text);
    }
    .hist-empty {
      color: var(--muted); font-size: 13px; padding: 48px 0;
      text-align: center;
    }
    .hist-table { width: 100%; border-collapse: collapse; }
    .hist-table th {
      text-align: left; font-size: 10px; font-weight: 700;
      text-transform: uppercase; letter-spacing: .7px;
      color: var(--muted); padding: 6px 12px 10px;
      border-bottom: 1px solid var(--border);
    }
    .hist-table td {
      padding: 10px 12px; border-bottom: 1px solid rgba(28,45,68,.5);
      vertical-align: middle;
    }
    .hist-table tr:last-child td { border-bottom: none; }
    .hist-table tr:hover td { background: rgba(255,255,255,.02); }
    .hist-topic {
      font-size: 13px; font-weight: 500;
      max-width: 360px; overflow: hidden;
      text-overflow: ellipsis; white-space: nowrap;
    }
    .hist-date   { font-size: 11px; color: var(--dim); white-space: nowrap; }
    .hist-models {
      font-size: 11px; color: var(--dim);
      max-width: 160px; overflow: hidden;
      text-overflow: ellipsis; white-space: nowrap;
    }
    .hist-actions { display: flex; gap: 6px; white-space: nowrap; }
    .btn-sm {
      font: 600 11px inherit; padding: 4px 10px;
      border-radius: 5px; cursor: pointer;
      border: 1px solid; transition: opacity .15s;
    }
    .btn-sm:hover { opacity: .75; }
    .btn-open { background: rgba(59,130,246,.1); color: var(--diag); border-color: rgba(59,130,246,.2); }
    .btn-del  { background: rgba(244,63,94,.07); color: #fb7185; border-color: rgba(244,63,94,.2); }

    /* Run status badges (history) */
    .rs {
      display: inline-flex; font-size: 10px; font-weight: 700;
      padding: 2px 7px; border-radius: 99px;
      text-transform: uppercase; letter-spacing: .4px; white-space: nowrap;
    }
    .rs-running     { background: rgba(59,130,246,.14); color: #7dd3fc; }
    .rs-completed   { background: rgba(16,185,129,.14); color: #34d399; }
    .rs-cancelled   { background: rgba(107,114,128,.14);color: #9ca3af; }
    .rs-error       { background: rgba(244,63,94,.14);  color: #fb7185; }
    .rs-interrupted { background: rgba(245,158,11,.14); color: #fbbf24; }

    /* ── Animation ──────────────────────────────────────────────── */
    @keyframes fadeUp { from{opacity:0;transform:translateY(9px)} to{opacity:1;transform:none} }

    /* ── Responsive ─────────────────────────────────────────────── */
    @media (max-width: 820px) {
      body { overflow: auto; }
      .app { grid-template-columns: 1fr; height: auto; }
      .sidebar { height: auto; max-height: 55vh; }
      .convo { height: 60vh; }
    }
  </style>
</head>
<body>
<div class="app">

  <!-- ── Sidebar ─────────────────────────────────── -->
  <aside class="sidebar">
    <div class="sidebar-head">
      <div class="brand">
        <div class="brand-icon">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
            <path d="M9.5 2A2.5 2.5 0 0 1 12 4.5v15a2.5 2.5 0 0 1-4.96-.44 2.5 2.5 0 0 1-2.96-3.08 3 3 0 0 1-.34-5.58 2.5 2.5 0 0 1 1.32-4.24 2.5 2.5 0 0 1 1.98-3A2.5 2.5 0 0 1 9.5 2Z"/>
            <path d="M14.5 2A2.5 2.5 0 0 0 12 4.5v15a2.5 2.5 0 0 0 4.96-.44 2.5 2.5 0 0 0 2.96-3.08 3 3 0 0 0 .34-5.58 2.5 2.5 0 0 0-1.32-4.24 2.5 2.5 0 0 0-1.98-3A2.5 2.5 0 0 0 14.5 2Z"/>
          </svg>
        </div>
        <div>
          <div class="brand-name">Agents Discussion</div>
          <div class="brand-sub">Diagnóstico técnico multiagente</div>
        </div>
      </div>
      <div id="status-pill" class="status-pill idle">
        <div class="dot"></div>
        <span id="status-text">Listo</span>
      </div>
    </div>

    <div class="sidebar-body">
      <form id="run-form">
        <div class="fsect-title">Incidente</div>

        <div class="field">
          <label for="topic">Descripción del problema</label>
          <textarea id="topic" name="topic"
            placeholder="Ej: El endpoint /orders tarda 8 s desde el último deploy" required></textarea>
        </div>

        <div class="field">
          <label for="incident_file">Logs / trazas del incidente</label>
          <input id="incident_file" name="incident_file" type="file">
        </div>

        <div class="field">
          <label for="base_context">Contexto base del sistema</label>
          <input id="base_context" name="base_context" type="file" multiple>
          <div class="field-hint">Arquitectura, SLAs, servicios, restricciones.</div>
        </div>

        <!-- ── Model selector ──────────────────────── -->
        <details class="adv" id="models-sect" open style="margin-top:10px">
          <summary>Modelos</summary>
          <div class="adv-body">
            <div class="field">
              <label for="sel-diag">Agente Diagnóstico</label>
              <select id="sel-diag" name="diagnostic_model" disabled>
                <option value="">Cargando modelos...</option>
              </select>
            </div>
            <div class="field">
              <label for="sel-skeptic">Revisor Escéptico</label>
              <select id="sel-skeptic" name="skeptic_model" disabled>
                <option value="">Cargando modelos...</option>
              </select>
            </div>
            <div class="field">
              <label for="sel-mod">Moderador</label>
              <select id="sel-mod" name="moderator_model" disabled>
                <option value="">Cargando modelos...</option>
              </select>
            </div>
          </div>
        </details>

        <!-- ── Advanced options ────────────────────── -->
        <details class="adv" style="margin-top:6px">
          <summary>Opciones avanzadas</summary>
          <div class="adv-body">
            <div class="fsect-title" style="margin-top:2px">Código fuente</div>
            <div class="field">
              <label for="project_path">Ruta del proyecto</label>
              <input id="project_path" name="project_path" type="text"
                placeholder="/home/usuario/sources/mi-app">
            </div>
            <div class="field">
              <label for="include_patterns">Patrones de inclusión</label>
              <input id="include_patterns" name="include_patterns" type="text"
                placeholder="src/**/*.py, tests/**/*.py">
            </div>
            <div class="two-col">
              <div class="field">
                <label for="max_files">Máx. archivos</label>
                <input id="max_files" name="max_files" type="number" min="1" value="20">
              </div>
              <div class="field">
                <label for="max_chars_per_file">Máx. chars/archivo</label>
                <input id="max_chars_per_file" name="max_chars_per_file" type="number" min="1" value="12000">
              </div>
            </div>
            <label class="checkbox-row">
              <input id="no_redact_context" name="no_redact_context" type="checkbox">
              <span>No redactar secretos del contexto</span>
            </label>
            <div class="fsect-title" style="margin-top:10px">SSH (opcional, para tools)</div>
            <div class="field">
              <label for="ssh_host">Host SSH</label>
              <input id="ssh_host" name="ssh_host" type="text" placeholder="servidor.ejemplo.com">
            </div>
            <div class="two-col">
              <div class="field">
                <label for="ssh_user">Usuario SSH</label>
                <input id="ssh_user" name="ssh_user" type="text" placeholder="admin">
              </div>
              <div class="field">
                <label for="ssh_port">Puerto</label>
                <input id="ssh_port" name="ssh_port" type="number" min="1" max="65535" value="22">
              </div>
            </div>
            <div class="field">
              <label for="ssh_key_path">Ruta clave privada</label>
              <input id="ssh_key_path" name="ssh_key_path" type="text" placeholder="~/.ssh/id_rsa">
              <div class="field-hint">Sobreescribe SSH_KEY_PATH del .env para este debate.</div>
            </div>
          </div>
        </details>

        <button id="start-button" class="btn-run" type="submit">Iniciar diagnóstico</button>
      </form>

      <button id="stop-button" class="btn-stop hidden" type="button">&#9632; Detener debate</button>
    </div>
  </aside>

  <!-- ── Conversation ──────────────────────────────── -->
  <section class="convo">

    <!-- Tab strip -->
    <div class="convo-tabs">
      <button class="tab-btn active" id="tab-debate">Debate</button>
      <button class="tab-btn" id="tab-hist">Historial</button>
    </div>

    <!-- Thread (debate view) -->
    <div id="thread">
      <div id="empty-state" class="empty-state">
        <svg width="52" height="52" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round">
          <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
        </svg>
        <h2>Sin debate activo</h2>
        <p>Define el problema en el formulario y lanza el diagnóstico para ver cómo razonan los agentes.</p>
      </div>

      <div id="typing-indicator" class="typing hidden">
        <div class="typing-dots"><span></span><span></span><span></span></div>
        <span id="typing-label" class="typing-lbl">Analizando...</span>
      </div>
    </div>

    <!-- History panel -->
    <div id="hist-panel">
      <!-- populated by JS loadHistory() -->
    </div>

  </section>
</div>

<script>
// ── Icons ──────────────────────────────────────────────────────────────
const ICO = {
  diag: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg>',
  skeptic: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z"/></svg>',
  rebuttal: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="m9 12 2 2 4-4"/></svg>',
  mod: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="m16 16 3-8 3 8c-.87.65-1.92 1-3 1s-2.13-.35-3-1z"/><path d="m2 16 3-8 3 8c-.87.65-1.92 1-3 1s-2.13-.35-3-1z"/><path d="M7 21h10M12 3v18M3 7h2c2 0 5-1 7-2 2 1 5 2 7 2h2"/></svg>',
  final: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><path d="m9 11 3 3L22 4"/></svg>',
  err: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3z"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg>',
  info: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/></svg>',
};

const AGENT_CFG = {
  diagnostic_agent:           { cls: 'a-diag',     ico: ICO.diag },
  skeptic_agent:              { cls: 'a-skeptic',   ico: ICO.skeptic },
  diagnostic_rebuttal_agent:  { cls: 'a-rebuttal',  ico: ICO.rebuttal },
};

const NEXT_LABEL = {
  diagnostic_agent:          'Revisor Escéptico',
  skeptic_agent:             'Contrarréplica',
  diagnostic_rebuttal_agent: 'Moderador',
};

const STATUS_CFG = {
  continue:               { lbl: 'Continuar',     cls: 'db-continue' },
  final_diagnosis:        { lbl: 'Diagnóstico',   cls: 'db-final_diagnosis' },
  needs_more_data:        { lbl: 'Faltan datos',  cls: 'db-needs_more_data' },
  propose_fix:            { lbl: 'Fix listo',     cls: 'db-propose_fix' },
  structured_uncertainty: { lbl: 'Incertidumbre', cls: 'db-structured_uncertainty' },
};

const RISK_CLS = { critical:'rc', high:'rh', medium:'rm', low:'rl' };

// ── State ──────────────────────────────────────────────────────────────
let source        = null;
let curRound      = 1;
let maxRounds     = 4;
let lastDecision  = null;
let currentRunId  = null;
let activeTab     = 'debate';

// ── DOM ────────────────────────────────────────────────────────────────
const form        = document.getElementById('run-form');
const thread      = document.getElementById('thread');
const histPanel   = document.getElementById('hist-panel');
const pill        = document.getElementById('status-pill');
const pillTxt     = document.getElementById('status-text');
const btn         = document.getElementById('start-button');
const stopBtn     = document.getElementById('stop-button');
const emptyState  = document.getElementById('empty-state');
const typing      = document.getElementById('typing-indicator');
const typingLbl   = document.getElementById('typing-label');
const tabDebate   = document.getElementById('tab-debate');
const tabHist     = document.getElementById('tab-hist');

// ── Helpers ────────────────────────────────────────────────────────────
const esc = s => String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');

function md(text) {
  try { return marked.parse(String(text||''), { breaks: true, gfm: true }); }
  catch { return '<p>' + esc(text) + '</p>'; }
}

function rlist(items) {
  if (!items || !items.length) return '<p class="msect-body it">—</p>';
  return '<ul class="mlist">' + items.map(i => '<li>' + esc(i) + '</li>').join('') + '</ul>';
}

function setStatus(state, text) {
  pill.className = 'status-pill ' + state;
  pillTxt.textContent = text;
}

function showTyping(label) {
  typingLbl.textContent = label + ' analizando...';
  typing.classList.remove('hidden');
  scrollBottom();
}
function hideTyping() { typing.classList.add('hidden'); }

function scrollBottom() { thread.scrollTop = thread.scrollHeight; }

function push(el) {
  thread.insertBefore(el, typing);
  scrollBottom();
}

function clearThread() {
  Array.from(thread.children).forEach(c => {
    if (c.id !== 'empty-state' && c.id !== 'typing-indicator') c.remove();
  });
  emptyState.classList.remove('hidden');
  hideTyping();
}

function addRoundSep(round, total) {
  const d = document.createElement('div');
  d.className = 'round-sep';
  d.innerHTML = '<span>Ronda ' + round + ' <em>de ' + total + '</em></span>';
  thread.insertBefore(d, typing);
}

function showStop() { stopBtn.classList.remove('hidden'); }
function hideStop()  { stopBtn.classList.add('hidden'); }

const CHEVRON = '<svg class="acard-chevron" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>';

// ── Tab switching ──────────────────────────────────────────────────────
tabDebate.addEventListener('click', showDebateTab);
tabHist.addEventListener('click', showHistoryTab);

function showDebateTab() {
  activeTab = 'debate';
  tabDebate.classList.add('active');
  tabHist.classList.remove('active');
  thread.style.display = 'flex';
  histPanel.classList.remove('active');
}

function showHistoryTab() {
  activeTab = 'history';
  tabHist.classList.add('active');
  tabDebate.classList.remove('active');
  thread.style.display = 'none';
  histPanel.classList.add('active');
  loadHistory();
}

// ── Stop / cancel ──────────────────────────────────────────────────────
stopBtn.addEventListener('click', stopRun);

async function stopRun() {
  if (!currentRunId) return;
  stopBtn.disabled = true;
  try {
    await fetch('/api/runs/' + currentRunId, { method: 'DELETE' });
  } catch (_) { /* server will timeout and clean up */ }
  // run_cancelled SSE event drives the UI update
}

// ── Card builders ──────────────────────────────────────────────────────
function buildAgentCard(event) {
  const cfg = AGENT_CFG[event.node] || { cls: 'a-diag', ico: ICO.diag };
  const el = document.createElement('article');
  el.className = 'acard ' + cfg.cls;
  el.innerHTML =
    '<div class="acard-head">' +
      '<div class="agent-id">' +
        '<div class="agent-ico">' + cfg.ico + '</div>' +
        '<div><span class="agent-nm">' + esc(event.role) + '</span>' +
             '<span class="agent-sub">Ronda ' + curRound + '</span></div>' +
      '</div>' +
      CHEVRON +
    '</div>' +
    '<div class="card-body">' + md(event.content) + '</div>';
  el.querySelector('.acard-head').addEventListener('click', () => el.classList.toggle('collapsed'));
  return el;
}

function buildModCard(decision, round) {
  lastDecision = decision;
  const st  = STATUS_CFG[decision.status] || { lbl: decision.status, cls: 'db-structured_uncertainty' };
  const pct = Math.round((decision.confidence || 0) * 100);
  const barColor = pct >= 80 ? '#10b981' : pct >= 50 ? '#f59e0b' : '#f43f5e';
  const rk  = RISK_CLS[decision.risk_level] || 'ru';

  const el = document.createElement('article');
  el.className = 'acard a-mod';

  let body = '';

  if (decision.leading_hypothesis) {
    body += '<div><div class="msect-title">Hipótesis principal</div>' +
            '<div class="msect-body">' + esc(decision.leading_hypothesis) + '</div></div>';
  }

  if (decision.next_step) {
    body += '<div><div class="msect-title">Siguiente paso</div>' +
            '<div class="msect-body">' + esc(decision.next_step) + '</div></div>';
  }

  const evRow = (decision.evidence && decision.evidence.length) || (decision.missing_evidence && decision.missing_evidence.length);
  if (evRow) {
    body += '<div class="mod-row2">';
    if (decision.evidence && decision.evidence.length) {
      body += '<div><div class="msect-title">Evidencias (' + decision.evidence.length + ')</div>' +
              rlist(decision.evidence) + '</div>';
    }
    if (decision.missing_evidence && decision.missing_evidence.length) {
      body += '<div><div class="msect-title">Evidencia faltante</div>' +
              rlist(decision.missing_evidence) + '</div>';
    }
    body += '</div>';
  }

  if (decision.recommended_fix) {
    body += '<div class="fix-box"><div class="msect-title">Fix recomendado</div>' +
            '<div class="msect-body">' + esc(decision.recommended_fix) + '</div></div>';
  }

  const rejRow = (decision.rejected_hypotheses && decision.rejected_hypotheses.length) || (decision.validation && decision.validation.length);
  if (rejRow) {
    body += '<div class="mod-row2">';
    if (decision.rejected_hypotheses && decision.rejected_hypotheses.length) {
      body += '<div><div class="msect-title">Hipótesis rechazadas</div>' +
              rlist(decision.rejected_hypotheses) + '</div>';
    }
    if (decision.validation && decision.validation.length) {
      body += '<div><div class="msect-title">Pasos de validación</div>' +
              rlist(decision.validation) + '</div>';
    }
    body += '</div>';
  }

  if (decision.stop_reason) {
    body += '<div><div class="msect-title">Motivo de cierre</div>' +
            '<div class="msect-body it">' + esc(decision.stop_reason) + '</div></div>';
  }

  el.innerHTML =
    '<div class="acard-head">' +
      '<div class="agent-id">' +
        '<div class="agent-ico">' + ICO.mod + '</div>' +
        '<div><span class="agent-nm">Moderador</span>' +
             '<span class="agent-sub">Ronda ' + round + ' · Decisión</span></div>' +
      '</div>' +
      '<div style="display:flex;align-items:center;gap:7px">' +
        '<span class="rbadge ' + rk + '">' + esc(decision.risk_level || '?') + '</span>' +
        '<span class="dbadge ' + st.cls + '">' + st.lbl + '</span>' +
        CHEVRON +
      '</div>' +
    '</div>' +
    '<div class="conf-row">' +
      '<span class="conf-lbl">Confianza</span>' +
      '<div class="conf-track"><div class="conf-fill" style="width:' + pct + '%;background:' + barColor + '"></div></div>' +
      '<span class="conf-val">' + pct + '%</span>' +
    '</div>' +
    '<div class="mod-body">' + body + '</div>';

  el.querySelector('.acard-head').addEventListener('click', () => el.classList.toggle('collapsed'));
  return el;
}

function buildFinalCard(decision) {
  const pct  = Math.round((decision.confidence || 0) * 100);
  const rk   = RISK_CLS[decision.risk_level] || 'ru';
  const st   = STATUS_CFG[decision.status] || { lbl: decision.status, cls: 'db-structured_uncertainty' };
  const rnds = curRound;

  let extra = '';
  if (decision.recommended_fix) {
    extra = '<div class="mod-body" style="border-top:1px solid var(--border)">' +
            '<div><div class="msect-title">Fix recomendado</div>' +
            '<div class="msect-body">' + esc(decision.recommended_fix) + '</div></div>' +
            '</div>';
  }

  const el = document.createElement('article');
  el.className = 'acard a-final';
  el.innerHTML =
    '<div class="acard-head">' +
      '<div class="agent-id">' +
        '<div class="agent-ico">' + ICO.final + '</div>' +
        '<div><span class="agent-nm">Diagnóstico completo</span>' +
             '<span class="agent-sub">Debate cerrado · ' + rnds + ' ronda' + (rnds > 1 ? 's' : '') + '</span></div>' +
      '</div>' +
      '<span class="dbadge ' + st.cls + '">' + st.lbl + '</span>' +
    '</div>' +
    '<div class="final-stats">' +
      '<div class="fstat"><span class="fstat-lbl">Confianza</span><span class="fstat-val">' + pct + '%</span></div>' +
      '<div class="fstat"><span class="fstat-lbl">Rondas</span><span class="fstat-val">' + rnds + '</span></div>' +
      '<div class="fstat"><span class="fstat-lbl">Riesgo</span><span class="fstat-val"><span class="rbadge ' + rk + '">' + esc(decision.risk_level || '?') + '</span></span></div>' +
    '</div>' +
    extra;

  return el;
}

function buildErrCard(msg) {
  const el = document.createElement('article');
  el.className = 'acard a-err';
  el.innerHTML =
    '<div class="acard-head">' +
      '<div class="agent-id">' +
        '<div class="agent-ico">' + ICO.err + '</div>' +
        '<div><span class="agent-nm">Error</span><span class="agent-sub">Fallo durante el debate</span></div>' +
      '</div>' +
    '</div>' +
    '<div class="card-body">' + esc(msg) + '</div>';
  return el;
}

function buildInfoCard(title, msg) {
  const el = document.createElement('article');
  el.className = 'acard a-info';
  el.innerHTML =
    '<div class="acard-head">' +
      '<div class="agent-id">' +
        '<div class="agent-ico">' + ICO.info + '</div>' +
        '<div><span class="agent-nm">' + esc(title) + '</span></div>' +
      '</div>' +
    '</div>' +
    '<div class="card-body">' + esc(msg) + '</div>';
  return el;
}

function buildToolCallCard(ev) {
  const isErr = !!ev.error;
  const card  = document.createElement('div');
  card.className = 'tc-card' + (isErr ? ' tc-err' : '');

  const toolIco = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>';
  const argsStr   = JSON.stringify(ev.args || {}, null, 2);
  const resultStr = String(ev.result || '');

  card.innerHTML =
    '<div class="tc-head">' +
      '<span class="tc-ico">' + toolIco + '</span>' +
      '<span class="tc-name">' + esc(ev.tool_name) + '</span>' +
      '<span style="font-size:11px;color:#64748b">' + esc(ev.agent_role || ev.agent_node) + '</span>' +
      '<span class="tc-arrow">&#9654;</span>' +
    '</div>' +
    '<div class="tc-body">' +
      '<div class="tc-section-lbl">Argumentos</div>' +
      '<pre class="tc-pre">' + esc(argsStr) + '</pre>' +
      '<div class="tc-section-lbl" style="margin-top:8px">Resultado' + (isErr ? ' (error)' : '') + '</div>' +
      '<pre class="tc-pre">' + esc(resultStr) + '</pre>' +
    '</div>';

  card.querySelector('.tc-head').addEventListener('click', () => {
    card.classList.toggle('open');
  });

  return card;
}

// ── Event rendering ────────────────────────────────────────────────────
function renderEvent(ev) {
  if (ev.type === 'tool_call') {
    const wrapper = document.createElement('div');
    wrapper.className = 'tc-wrapper';
    wrapper.appendChild(buildToolCallCard(ev));
    thread.insertBefore(wrapper, typing);
    scrollBottom();
    return;
  }

  hideTyping();

  if (ev.type === 'run_started') {
    maxRounds = ev.max_rounds;
    curRound  = 1;
    setStatus('running', 'Ronda 1 de ' + maxRounds);

    const hdr = document.createElement('div');
    hdr.className = 'run-header';
    hdr.innerHTML =
      '<div class="run-header-label">Diagnóstico en curso</div>' +
      '<div class="run-header-topic">' + esc(ev.topic) + '</div>' +
      '<div class="run-header-meta">Máximo ' + ev.max_rounds + ' rondas &middot; Confianza requerida: ' +
        Math.round(ev.confidence_threshold * 100) + '%</div>';
    thread.insertBefore(hdr, typing);

    addRoundSep(1, maxRounds);
    showTyping('Diagnóstico Principal');

  } else if (ev.type === 'agent_completed') {
    push(buildAgentCard(ev));
    const next = NEXT_LABEL[ev.node];
    if (next) showTyping(next);

  } else if (ev.type === 'moderator_decision') {
    const d = ev.decision || {};
    push(buildModCard(d, curRound));

    if (d.status === 'continue') {
      curRound = ev.round || (curRound + 1);
      addRoundSep(curRound, maxRounds);
      setStatus('running', 'Ronda ' + curRound + ' de ' + maxRounds);
      showTyping('Diagnóstico Principal');
    }

  } else if (ev.type === 'final_result') {
    if (lastDecision) push(buildFinalCard(lastDecision));

  } else if (ev.type === 'run_finished') {
    setStatus('done', 'Finalizado');
    btn.disabled = false;
    hideStop();
    currentRunId = null;
    if (source) { source.close(); source = null; }

  } else if (ev.type === 'run_cancelled') {
    push(buildInfoCard('Debate detenido', 'El debate fue interrumpido manualmente.'));
    setStatus('idle', 'Detenido');
    btn.disabled = false;
    hideStop();
    stopBtn.disabled = false;
    currentRunId = null;
    if (source) { source.close(); source = null; }

  } else if (ev.type === 'error') {
    push(buildErrCard(ev.message));
    setStatus('error', 'Error');
    btn.disabled = false;
    hideStop();
    currentRunId = null;
    if (source) { source.close(); source = null; }
  }
}

// ── Form submit ────────────────────────────────────────────────────────
form.addEventListener('submit', async ev => {
  ev.preventDefault();
  if (source) { source.close(); source = null; }

  showDebateTab();
  clearThread();
  emptyState.classList.add('hidden');
  lastDecision = null;
  curRound = 1;
  currentRunId = null;

  setStatus('preparing', 'Preparando...');
  btn.disabled = true;
  showStop();

  try {
    const res     = await fetch('/api/runs', { method: 'POST', body: new FormData(form) });
    const payload = await res.json();
    if (!res.ok) throw new Error(payload.detail || 'No se pudo iniciar el diagnóstico');

    currentRunId = payload.run_id;
    source = new EventSource('/api/runs/' + payload.run_id + '/events');
    source.onmessage = msg => renderEvent(JSON.parse(msg.data));
    source.onerror   = () => {
      hideTyping();
      push(buildErrCard('Se perdió la conexión con el servidor.'));
      setStatus('error', 'Error de conexión');
      btn.disabled = false;
      hideStop();
      currentRunId = null;
      if (source) { source.close(); source = null; }
    };
  } catch (err) {
    push(buildErrCard(err.message));
    setStatus('error', 'Error');
    btn.disabled = false;
    hideStop();
    currentRunId = null;
  }
});

// ── History ────────────────────────────────────────────────────────────
async function loadHistory() {
  histPanel.innerHTML = '<p class="hist-empty">Cargando historial...</p>';
  try {
    const res  = await fetch('/api/runs');
    const data = await res.json();
    renderHistoryList(data.runs || []);
  } catch {
    histPanel.innerHTML = '<p class="hist-empty">Error al cargar el historial.</p>';
  }
}

const RS_CFG = {
  running:     ['rs-running',     'En curso'],
  completed:   ['rs-completed',   'Completado'],
  cancelled:   ['rs-cancelled',   'Detenido'],
  error:       ['rs-error',       'Error'],
  interrupted: ['rs-interrupted', 'Interrumpido'],
};

function statusBadge(status) {
  const [cls, lbl] = RS_CFG[status] || ['rs-cancelled', status || '?'];
  return '<span class="rs ' + cls + '">' + esc(lbl) + '</span>';
}

function renderHistoryList(runs) {
  if (!runs.length) {
    histPanel.innerHTML = '<p class="hist-empty">No hay debates guardados todavía. Lanza un diagnóstico para verlo aquí.</p>';
    return;
  }

  let html = '<div class="hist-title">Historial de debates</div>' +
    '<table class="hist-table"><thead><tr>' +
    '<th>Tema</th><th>Modelos</th><th>Fecha</th><th>Estado</th><th></th>' +
    '</tr></thead><tbody>';

  for (const r of runs) {
    const date = r.timestamp
      ? new Date(r.timestamp).toLocaleString('es', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
      : '—';
    const mods = r.models
      ? [r.models.diagnostic, r.models.skeptic]
          .filter(Boolean)
          .map(m => m.replace('copilot/', '').replace('openai/', ''))
          .join(', ')
      : '—';
    const rid = esc(r.run_id || '');

    html += '<tr>' +
      '<td><div class="hist-topic" title="' + esc(r.topic) + '">' + esc(r.topic || '—') + '</div></td>' +
      '<td><div class="hist-models" title="' + esc(mods) + '">' + esc(mods) + '</div></td>' +
      '<td><span class="hist-date">' + esc(date) + '</span></td>' +
      '<td>' + statusBadge(r.status) + '</td>' +
      '<td><div class="hist-actions">' +
        '<button class="btn-sm btn-open" onclick="replayRun(\\'' + rid + '\\')">Abrir</button>' +
        '<button class="btn-sm btn-del"  onclick="deleteRun(\\'' + rid + '\\', this)">Eliminar</button>' +
      '</div></td>' +
      '</tr>';
  }

  html += '</tbody></table>';
  histPanel.innerHTML = html;
}

async function replayRun(runId) {
  let data;
  try {
    const res = await fetch('/api/runs/' + runId);
    if (!res.ok) { alert('No se pudo cargar el debate.'); return; }
    data = await res.json();
  } catch (err) {
    alert('Error al cargar: ' + err.message);
    return;
  }

  showDebateTab();
  clearThread();
  emptyState.classList.add('hidden');
  lastDecision = null;
  curRound = 1;

  // Replay banner
  const banner = document.createElement('div');
  banner.className = 'replay-banner';
  banner.innerHTML = '&#9654; Reproduciendo debate &nbsp;&middot;&nbsp; <strong>' + esc(data.topic || '') + '</strong>';
  thread.insertBefore(banner, typing);

  for (const ev of (data.events || [])) {
    renderEvent(ev);
  }

  hideTyping();
  scrollBottom();
}

async function deleteRun(runId, btnEl) {
  if (!confirm('¿Eliminar este debate del historial?')) return;
  try {
    await fetch('/api/runs/' + runId, { method: 'DELETE' });
    const row = btnEl.closest('tr');
    if (row) row.remove();
    if (!histPanel.querySelector('tr')) {
      histPanel.innerHTML = '<p class="hist-empty">No hay debates guardados todavía.</p>';
    }
  } catch (err) {
    alert('Error al eliminar: ' + err.message);
  }
}

// ── Model selector ─────────────────────────────────────────────────────
async function loadModels() {
  try {
    const [mRes, sRes] = await Promise.all([
      fetch('/api/models'),
      fetch('/api/settings'),
    ]);
    const { models = [] } = await mRes.json();
    const settings = sRes.ok ? await sRes.json() : {};

    populateSelect('sel-diag',    models, settings.diagnostic_model || '');
    populateSelect('sel-skeptic', models, settings.skeptic_model    || '');
    populateSelect('sel-mod',     models, settings.moderator_model  || '');
  } catch (err) {
    console.warn('Model list unavailable:', err.message);
    for (const id of ['sel-diag', 'sel-skeptic', 'sel-mod']) {
      const sel = document.getElementById(id);
      if (sel) {
        sel.innerHTML = '<option value="">— Desde .env —</option>';
        sel.disabled = false;
      }
    }
  }
}

const PROV_LABELS = {
  copilot:       'GitHub Copilot',
  github_models: 'GitHub Models',
};

function populateSelect(id, models, selected) {
  const sel = document.getElementById(id);
  if (!sel) return;
  sel.disabled = false;

  const groups = {};
  for (const m of models) {
    const p = m.provider || 'other';
    (groups[p] = groups[p] || []).push(m);
  }

  sel.innerHTML = '';

  // Default "from .env" option
  const dflt = document.createElement('option');
  dflt.value = '';
  dflt.textContent = '— Desde .env —';
  if (!selected) dflt.selected = true;
  sel.appendChild(dflt);

  for (const [prov, items] of Object.entries(groups)) {
    const grp = document.createElement('optgroup');
    grp.label = PROV_LABELS[prov] || prov;
    for (const m of items) {
      const opt = document.createElement('option');
      opt.value = m.id;
      opt.textContent = m.name || m.id;
      if (m.id === selected) opt.selected = true;
      grp.appendChild(opt);
    }
    sel.appendChild(grp);
  }
}

// ── Init ───────────────────────────────────────────────────────────────
loadModels();
</script>
</body>
</html>
"""


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return HTML


@app.get("/api/settings")
async def settings_api() -> JSONResponse:
    try:
        s = get_settings()
        return JSONResponse({
            "diagnostic_model":    s.diagnostic_model,
            "skeptic_model":       s.skeptic_model,
            "moderator_model":     s.moderator_model,
            "max_rounds":          s.max_rounds,
            "confidence_threshold": s.confidence_threshold,
        })
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=500)


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


@app.get("/api/runs")
async def list_runs_api() -> JSONResponse:
    return JSONResponse({"runs": store.list_runs()})


@app.post("/api/runs")
async def create_run(
    topic: Annotated[str, Form()],
    diagnostic_model: Annotated[str, Form()] = "",
    skeptic_model:    Annotated[str, Form()] = "",
    moderator_model:  Annotated[str, Form()] = "",
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

    models = {
        "diagnostic": diagnostic_model or settings.diagnostic_model,
        "skeptic":    skeptic_model    or settings.skeptic_model,
        "moderator":  moderator_model  or settings.moderator_model,
    }
    run_id    = uuid.uuid4().hex
    timestamp = datetime.now(timezone.utc).isoformat()

    RUNS[run_id] = {
        "topic":       topic,
        "context":     context,
        "ssh_defaults": {
            "host":     ssh_host.strip(),
            "user":     ssh_user.strip(),
            "port":     ssh_port,
            "key_path": ssh_key_path.strip(),
        },
        "models":    models,
        "timestamp": timestamp,
        "events":    [],
        "cancelled": False,
        "status":    "running",
    }
    store.create(run_id, topic, timestamp, models)
    return JSONResponse({"run_id": run_id})


@app.get("/api/runs/{run_id}/events")
def run_events(run_id: str) -> StreamingResponse:
    run = RUNS.get(run_id)
    if run is None:
        return StreamingResponse(
            _single_error_event("Run not found."), media_type="text/event-stream"
        )
    return StreamingResponse(
        _event_stream(run["topic"], run["context"], run.get("ssh_defaults"), run_id, run.get("models", {})),
        media_type="text/event-stream",
    )


@app.get("/api/runs/{run_id}")
async def get_run_api(run_id: str) -> JSONResponse:
    # Check live in-memory run first
    run = RUNS.get(run_id)
    if run:
        return JSONResponse({
            "run_id":    run_id,
            "topic":     run["topic"],
            "timestamp": run.get("timestamp", ""),
            "status":    "running",
            "models":    run.get("models", {}),
            "events":    run.get("events", []),
        })
    data = store.get(run_id)
    if data is None:
        return JSONResponse({"detail": "Run not found."}, status_code=404)
    return JSONResponse(data)


@app.delete("/api/runs/{run_id}")
async def delete_run_api(run_id: str) -> JSONResponse:
    run = RUNS.get(run_id)
    if run:
        # Running — signal cancellation; _event_stream will flush to disk and clean up
        run["cancelled"] = True
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


async def _save_upload(upload: UploadFile, destination: Path) -> Path:
    content = await upload.read()
    destination.write_bytes(content)
    return destination


def _split_patterns(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


async def _event_stream(
    topic: str,
    context: str,
    ssh_defaults: dict | None,
    run_id: str,
    models: dict[str, str],
):
    # Inject SSH defaults into context so the LLM uses them as tool-call arguments.
    if ssh_defaults:
        lines = []
        if ssh_defaults.get("host"):
            lines.append(f"Default SSH host: {ssh_defaults['host']}")
        if ssh_defaults.get("user"):
            lines.append(f"Default SSH user: {ssh_defaults['user']}")
        if ssh_defaults.get("port") and ssh_defaults["port"] != 22:
            lines.append(f"Default SSH port: {ssh_defaults['port']}")
        if ssh_defaults.get("key_path"):
            lines.append(f"Default SSH key path: {ssh_defaults['key_path']}")
        if lines:
            ssh_block = "\n=== SSH Connection Defaults ===\n" + "\n".join(lines)
            context = (context + ssh_block) if context else ssh_block.lstrip()

    run_data = RUNS.get(run_id, {})

    def _flush(status: str) -> None:
        store.save(run_id, {
            "run_id":    run_id,
            "topic":     run_data.get("topic", topic),
            "timestamp": run_data.get("timestamp", ""),
            "status":    status,
            "models":    run_data.get("models", {}),
            "context":   run_data.get("context", context),
            "events":    run_data.get("events", []),
        })
        RUNS.pop(run_id, None)

    try:
        for event in stream_debate_events(
            topic,
            context,
            diagnostic_model=models.get("diagnostic", ""),
            skeptic_model=models.get("skeptic", ""),
            moderator_model=models.get("moderator", ""),
        ):
            # Check cancellation flag (set by DELETE /api/runs/{run_id})
            if run_data.get("cancelled"):
                _flush("cancelled")
                yield _sse({"type": "run_cancelled"})
                return

            if run_id in RUNS:
                RUNS[run_id]["events"].append(event)

            yield _sse(event)
            await asyncio.sleep(0)

            if event.get("type") == "run_finished":
                _flush("completed")

    except Exception as exc:  # noqa: BLE001
        error_event = {"type": "error", "message": str(exc)}
        if run_id in RUNS:
            RUNS[run_id]["events"].append(error_event)
        _flush("error")
        yield _sse(error_event)


async def _single_error_event(message: str):
    yield _sse({"type": "error", "message": message})


def _sse(payload: dict[str, object]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def main() -> None:
    uvicorn.run("agents_discussion.web:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    main()
