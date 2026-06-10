# Agents Discussion

Sistema multiagente para diagnosticar problemas técnicos, rendimiento y fixes de código. Usa LangGraph y tres agentes con modelos distintos consumidos mediante GitHub Models o GitHub Copilot.

## Agentes

- Diagnóstico Principal: propone la causa técnica más probable y un experimento mínimo.
- Revisor Escéptico: intenta falsar la hipótesis, propone alternativas y evalúa riesgos.
- Moderador / Tech Lead: decide si continuar, pedir más datos, recomendar fix o cerrar con incertidumbre estructurada. Recibe el historial completo del debate y devuelve una decisión estructurada (structured output con fallback a parseo JSON).

Los agentes citan la evidencia instrumental con el formato `[tool:<nombre>]` y el moderador pondera la confianza según la calidad de la evidencia.

## Instalación

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

### Docker

```bash
cp .env.example .env   # edita credenciales y modelos
docker compose up --build
```

El estado persistente (runs, audit log, plantillas personalizadas) vive en el volumen `/data`. Para exponerlo a más usuarios ponlo detrás de un reverse proxy con TLS y autenticación.

## Configuración

```bash
cp .env.example .env
```

Variables principales:

```env
GITHUB_TOKEN=ghp_your_token_here          # solo para GitHub Models
COPILOT_TOKEN=ghu_xxx                     # solo para modelos copilot/*

DIAGNOSTIC_MODEL=copilot/gpt-4o
SKEPTIC_MODEL=copilot/claude-sonnet-4.6
MODERATOR_MODEL=copilot/claude-sonnet-4.6

MAX_ROUNDS=4
CONFIDENCE_THRESHOLD=0.8

PROMPT_TEMPLATE=default                   # default|performance|errors|data|security
PROMPT_LANGUAGE=es                        # es|en

TOOL_APPROVAL_REQUIRED=true
APPROVAL_REQUIRED_TOOLS=run_ssh_command,run_local_command,run_kubectl,run_db_explain

WEB_HOST=127.0.0.1
WEB_PORT=8000
```

Ver `.env.example` para la lista completa (timeouts de aprobación, endpoints de observabilidad, etc.).

## Plantillas de prompts

Los system prompts están versionados en YAML por tipo de incidente e idioma:

- `default` — diagnóstico general
- `performance` — degradación de rendimiento y latencia
- `errors` — errores 5xx, excepciones y fallos intermitentes
- `data` — inconsistencias de datos, duplicados y corrupción
- `security` — incidentes de seguridad y accesos anómalos

Cada una existe en `es` y `en`. Las plantillas integradas viven en `src/agents_discussion/prompt_templates/`. Un administrador puede **añadir o sobreescribir** plantillas colocando ficheros `<nombre>.<lang>.yaml` en `PROMPTS_DIR` (por defecto `~/.local/share/agents-discussion/prompts`); los ficheros personalizados tienen prioridad sobre los integrados con el mismo nombre+idioma. Formato:

```yaml
name: mi-plantilla
language: es
version: 1
description: Descripción visible en la UI
diagnostic_system: |
  ...
skeptic_system: |
  ...
moderator_system: |
  ...
```

## Herramientas de diagnóstico

Los agentes disponen de tools en un bucle ReAct (`TOOLS_ENABLED=true`):

| Tool | Descripción | Aprobación por defecto |
|---|---|---|
| `run_ssh_command` | Comando remoto por SSH | manual |
| `run_local_command` | Comando shell local | manual |
| `run_kubectl` | kubectl solo lectura (get/describe/logs/top/...) | manual |
| `run_db_explain` | `EXPLAIN` de un SELECT vía psql (sin ejecutar la query) | manual |
| `http_get` | GET a health endpoints / APIs internas | automática |
| `query_prometheus` | Consulta PromQL instantánea (`PROMETHEUS_URL`) | automática |
| `query_loki` | Consulta LogQL de rango (`LOKI_URL`) | automática |
| `query_elasticsearch` | `_search` de solo lectura (`ELASTICSEARCH_URL`) | automática |
| `git_recent_changes` | Commits recientes + diffstat (diff de deploys) | automática |

`ENABLED_TOOLS` limita qué tools se exponen; `APPROVAL_REQUIRED_TOOLS` define cuáles requieren aprobación.

### Aprobación humana y auditoría

En la web, las tools sensibles se **pausan hasta que el operador las aprueba o rechaza** desde la propia conversación (timeout configurable con `APPROVAL_TIMEOUT_SECONDS`; sin respuesta no se ejecutan). Cada invocación —aprobada, rechazada o automática— queda registrada en `DATA_DIR/audit.jsonl` con timestamp, run, agente, argumentos y resultado.

## Uso

### CLI

```bash
agents-discuss "El endpoint /orders tarda 8s desde el último deploy"
agents-discuss "Diagnosticar degradación" --file incident.md --base-context arch.md
agents-discuss "Diagnosticar lentitud en /orders" --project ./backend --include "src/**/*.py"
```

La CLI no aplica gating de aprobación (las tools se ejecutan directamente), pero sí audita. Opciones: `--base-context` (repetible), `--no-redact-context`, `--max-files`, `--max-chars-per-file`, `--show-history`.

### Web

```bash
agents-discuss-web        # http://127.0.0.1:8000 (configurable con WEB_HOST/WEB_PORT)
```

La ejecución del debate corre **en background en el servidor**: cerrar o refrescar el navegador no detiene el debate, puedes reconectarte desde el historial («Ver en vivo») y varios espectadores pueden seguir el mismo run.

Desde la UI puedes:

- elegir tipo de incidente (plantilla de prompts) e idioma (es/en)
- elegir modelo por agente
- activar **aprobación manual de herramientas** (por defecto según `TOOL_APPROVAL_REQUIRED`)
- activar **pausa entre rondas** (human-in-the-loop): el debate se detiene tras cada decisión `continue` del moderador y puedes inyectar un comentario o dato que entra en el historial de la siguiente ronda
- **detener** un debate en curso
- **exportar el informe** completo del debate a Markdown
- **reanudar un debate cerrado con nueva evidencia**: si el moderador cerró con `needs_more_data` (o quieres aportar más datos), el nuevo debate parte del historial completo del anterior más la evidencia aportada

API principal:

```text
POST   /api/runs                      iniciar debate (form multipart)
GET    /api/runs                      historial
GET    /api/runs/{id}                 run completo (vivo o terminado)
GET    /api/runs/{id}/events          SSE (suscripción en vivo o replay)
GET    /api/runs/{id}/report          informe Markdown descargable
POST   /api/runs/{id}/resume          reanudar con nueva evidencia
POST   /api/runs/{id}/approval        resolver aprobación de tool {call_id, approved}
POST   /api/runs/{id}/comment         comentario entre rondas {comment}
DELETE /api/runs/{id}                 cancelar (en curso) o borrar (terminado)
GET    /api/prompts                   plantillas disponibles
```

## Lectura de proyectos

Cuando usas `--project` (o «Ruta del proyecto» en la web), se construye un contexto con archivos del proyecto. Sin `--include` se usan patrones comunes (`README*`, `pyproject.toml`, `package.json`, `src/**/*`, `tests/**/*`, ...). Se ignoran `.git`, `.venv`, `node_modules`, `dist`, `build`, etc. Límites: `--max-files` (20) y `--max-chars-per-file` (12000).

Nota: la ruta se lee del disco de la máquina donde corre el servidor. Úsalo como herramienta local o restringe el acceso si lo expones en red.

## Contexto base y redacción de secretos

`--base-context` orienta a los agentes con información estable (arquitectura, servicios, SLAs, restricciones). Por defecto se redactan valores que parecen secretos (`password`, `token`, `api_key`, credenciales en URIs) antes de enviarlos a los modelos; desactivable con `--no-redact-context`.

## Criterios de parada

- El moderador emite un estado distinto de `continue`.
- La confianza alcanza `CONFIDENCE_THRESHOLD`.
- Se alcanza `MAX_ROUNDS`.
- Faltan datos críticos (`needs_more_data` — reanudable después con nueva evidencia).
- Existe un fix mínimo suficientemente claro o un desacuerdo estructurado.

## Salida

La decisión final incluye: estado, confianza, riesgo, hipótesis principal, evidencia, evidencia faltante, hipótesis rechazadas, siguiente paso, fix recomendado, validación y motivo de cierre. El informe Markdown exportable añade todos los turnos, tool calls y un resumen ejecutivo.
