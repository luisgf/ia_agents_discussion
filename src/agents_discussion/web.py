import asyncio
import json
import tempfile
import uuid
from pathlib import Path
from typing import Annotated

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

app = FastAPI(title="Agents Discussion Web")
RUNS: dict[str, dict[str, str]] = {}


HTML = """
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Agents Discussion</title>
  <style>
    :root { color-scheme: dark; --bg: #0b1020; --panel: #121a2f; --muted: #95a3b8; --text: #e7edf7; --accent: #7dd3fc; --border: #26324a; }
    * { box-sizing: border-box; }
    body { margin: 0; font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: radial-gradient(circle at top left, #172554, var(--bg) 42%); color: var(--text); }
    main { width: min(1180px, calc(100vw - 32px)); margin: 32px auto; display: grid; grid-template-columns: 380px 1fr; gap: 20px; }
    h1 { margin: 0 0 8px; font-size: 28px; }
    p { color: var(--muted); line-height: 1.45; }
    form, .output { background: rgba(18, 26, 47, .88); border: 1px solid var(--border); border-radius: 18px; padding: 18px; box-shadow: 0 20px 60px rgba(0,0,0,.28); }
    label { display: block; margin: 14px 0 6px; color: #cbd5e1; font-size: 13px; font-weight: 650; }
    input, textarea { width: 100%; border: 1px solid var(--border); border-radius: 10px; background: #0f172a; color: var(--text); padding: 10px 12px; font: inherit; }
    textarea { min-height: 92px; resize: vertical; }
    input[type="file"] { padding: 9px; }
    button { width: 100%; margin-top: 16px; border: 0; border-radius: 12px; padding: 12px 14px; background: linear-gradient(135deg, #38bdf8, #818cf8); color: #020617; font-weight: 800; cursor: pointer; }
    button:disabled { opacity: .55; cursor: not-allowed; }
    .hint { font-size: 12px; color: var(--muted); margin-top: 6px; }
    .topbar { display: flex; align-items: start; justify-content: space-between; gap: 12px; margin-bottom: 12px; }
    .status { color: var(--accent); font-size: 13px; font-weight: 700; }
    .events { display: flex; flex-direction: column; gap: 14px; max-height: calc(100vh - 170px); overflow: auto; padding-right: 4px; }
    .card { border: 1px solid var(--border); border-left: 4px solid var(--accent); background: #0f172a; border-radius: 14px; padding: 14px; }
    .card h3 { margin: 0 0 8px; font-size: 15px; }
    .card pre { white-space: pre-wrap; word-break: break-word; margin: 0; color: #dbeafe; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 13px; line-height: 1.45; }
    .moderator { border-left-color: #fbbf24; }
    .final { border-left-color: #34d399; }
    .error { border-left-color: #fb7185; }
    @media (max-width: 860px) { main { grid-template-columns: 1fr; margin: 18px auto; } .events { max-height: none; } }
  </style>
</head>
<body>
  <main>
    <section>
      <h1>Agents Discussion</h1>
      <p>Diagnóstico técnico multiagente con conversación en tiempo real.</p>
      <form id="run-form">
        <label for="topic">Tema</label>
        <textarea id="topic" name="topic" placeholder="Ej: El endpoint /orders tarda 8s desde el último deploy" required></textarea>

        <label for="base_context">Contexto base</label>
        <input id="base_context" name="base_context" type="file" multiple>
        <div class="hint">Arquitectura, servicios, puertos no secretos, SLAs, restricciones.</div>

        <label for="incident_file">Incidente/logs</label>
        <input id="incident_file" name="incident_file" type="file">

        <label for="project_path">Ruta de proyecto</label>
        <input id="project_path" name="project_path" placeholder="/Users/tu_usuario/sources/app">

        <label for="include_patterns">Includes</label>
        <input id="include_patterns" name="include_patterns" placeholder="src/**/*.py, tests/**/*.py">

        <label for="max_files">Máximo de archivos</label>
        <input id="max_files" name="max_files" type="number" min="1" value="20">

        <label for="max_chars_per_file">Máximo chars por archivo</label>
        <input id="max_chars_per_file" name="max_chars_per_file" type="number" min="1" value="12000">

        <label><input id="no_redact_context" name="no_redact_context" type="checkbox" style="width:auto"> No redactar contexto</label>

        <button id="start-button" type="submit">Iniciar diagnóstico</button>
      </form>
    </section>

    <section class="output">
      <div class="topbar">
        <div>
          <h1>Conversación</h1>
          <p>Los turnos aparecen cuando cada agente termina.</p>
        </div>
        <div id="status" class="status">Listo</div>
      </div>
      <div id="events" class="events"></div>
    </section>
  </main>

  <script>
    const form = document.getElementById('run-form');
    const eventsEl = document.getElementById('events');
    const statusEl = document.getElementById('status');
    const button = document.getElementById('start-button');
    let source = null;

    function addCard(title, content, className = '') {
      const card = document.createElement('div');
      card.className = `card ${className}`;
      const h3 = document.createElement('h3');
      h3.textContent = title;
      const pre = document.createElement('pre');
      pre.textContent = content || '';
      card.appendChild(h3);
      card.appendChild(pre);
      eventsEl.appendChild(card);
      eventsEl.scrollTop = eventsEl.scrollHeight;
    }

    function renderEvent(event) {
      if (event.type === 'run_started') {
        statusEl.textContent = `Ejecutando · máximo ${event.max_rounds} rondas`;
        addCard('Run iniciado', `Tema: ${event.topic}\nUmbral de confianza: ${event.confidence_threshold}`);
      } else if (event.type === 'agent_completed') {
        addCard(event.role, event.content);
      } else if (event.type === 'moderator_decision') {
        const decision = event.decision || {};
        addCard('Moderador', JSON.stringify(decision, null, 2), 'moderator');
      } else if (event.type === 'final_result') {
        addCard('Resultado final', event.content, 'final');
      } else if (event.type === 'run_finished') {
        statusEl.textContent = 'Finalizado';
        button.disabled = false;
        if (source) source.close();
      } else if (event.type === 'error') {
        addCard('Error', event.message, 'error');
        statusEl.textContent = 'Error';
        button.disabled = false;
        if (source) source.close();
      }
    }

    form.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      if (source) source.close();
      eventsEl.innerHTML = '';
      statusEl.textContent = 'Preparando contexto';
      button.disabled = true;

      const data = new FormData(form);
      try {
        const response = await fetch('/api/runs', { method: 'POST', body: data });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.detail || 'No se pudo crear el run');
        source = new EventSource(`/api/runs/${payload.run_id}/events`);
        source.onmessage = (message) => renderEvent(JSON.parse(message.data));
        source.onerror = () => {
          addCard('Error', 'Se perdió la conexión SSE.', 'error');
          statusEl.textContent = 'Error';
          button.disabled = false;
          if (source) source.close();
        };
      } catch (error) {
        addCard('Error', error.message, 'error');
        statusEl.textContent = 'Error';
        button.disabled = false;
      }
    });
  </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return HTML


@app.post("/api/runs")
async def create_run(
    topic: Annotated[str, Form()],
    project_path: Annotated[str, Form()] = "",
    include_patterns: Annotated[str, Form()] = "",
    max_files: Annotated[int, Form()] = 20,
    max_chars_per_file: Annotated[int, Form()] = 12_000,
    no_redact_context: Annotated[bool, Form()] = False,
    incident_file: Annotated[UploadFile | None, File()] = None,
    base_context: Annotated[list[UploadFile] | None, File()] = None,
) -> JSONResponse:
    try:
        get_settings()
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
    except Exception as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    run_id = uuid.uuid4().hex
    RUNS[run_id] = {"topic": topic, "context": context}
    return JSONResponse({"run_id": run_id})


@app.get("/api/runs/{run_id}/events")
def run_events(run_id: str) -> StreamingResponse:
    run = RUNS.get(run_id)
    if run is None:
        return StreamingResponse(_single_error_event("Run not found."), media_type="text/event-stream")

    return StreamingResponse(_event_stream(run["topic"], run["context"]), media_type="text/event-stream")


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


async def _event_stream(topic: str, context: str):
    try:
        for event in stream_debate_events(topic, context):
            yield _sse(event)
            await asyncio.sleep(0)
    except Exception as exc:
        yield _sse({"type": "error", "message": str(exc)})


async def _single_error_event(message: str):
    yield _sse({"type": "error", "message": message})


def _sse(payload: dict[str, object]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def main() -> None:
    uvicorn.run("agents_discussion.web:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    main()
