# Agents Discussion

Sistema multiagente para diagnosticar problemas técnicos, rendimiento y fixes de código. Usa LangGraph y tres agentes con modelos distintos consumidos mediante el endpoint OpenAI-compatible de GitHub Models.

## Agentes

- Diagnóstico Principal: propone la causa técnica más probable y un experimento mínimo.
- Revisor Escéptico: intenta falsar la hipótesis, propone alternativas y evalúa riesgos.
- Moderador / Tech Lead: decide si continuar, pedir más datos, recomendar fix o cerrar con incertidumbre estructurada.

## Instalación

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Configuración

```bash
cp .env.example .env
```

Edita `.env`:

```env
GITHUB_TOKEN=ghp_your_token_here
GITHUB_MODELS_BASE_URL=https://models.github.ai/inference

DIAGNOSTIC_MODEL=openai/gpt-4.1
SKEPTIC_MODEL=anthropic/claude-3.5-sonnet
MODERATOR_MODEL=google/gemini-2.0-flash

MAX_ROUNDS=4
CONFIDENCE_THRESHOLD=0.8
```

Nota: necesitas acceso habilitado a GitHub Models para los modelos configurados. Si un modelo no está disponible para tu cuenta, cambia su identificador en `.env`.

## Uso

### CLI

```bash
agents-discuss "El endpoint /orders tarda 8s desde el último deploy"
```

Con contexto desde archivo:

```bash
agents-discuss "Diagnosticar degradación de rendimiento" --file incident.md
```

Con contexto base estable del sistema:

```bash
agents-discuss "Diagnosticar timeouts en creación de órdenes" \
  --base-context examples/base-context.example.md \
  --file incident.md
```

Puedes pasar varios archivos de contexto base:

```bash
agents-discuss "Diagnosticar errores intermitentes de pagos" \
  --base-context architecture.md \
  --base-context runtime-constraints.md \
  --file incident.md
```

Leyendo código fuente de un proyecto:

```bash
agents-discuss "Diagnosticar lentitud en /orders" --project ./backend
```

Limitando los archivos incluidos con patrones glob:

```bash
agents-discuss "Revisar rendimiento del endpoint de órdenes" \
  --project ./backend \
  --include "src/**/*.py" \
  --include "tests/**/*.py"
```

Controlando límites de contexto:

```bash
agents-discuss "Diagnosticar bug de autenticación" \
  --project ./app \
  --max-files 12 \
  --max-chars-per-file 8000
```

Mostrar todos los turnos:

```bash
agents-discuss "El servicio consume CPU al 100%" --file logs.md --show-history
```

## Lectura de proyectos

Cuando usas `--project`, la CLI construye un contexto simple con archivos del proyecto y lo pasa a los agentes. Si no indicas `--include`, se usan patrones comunes:

- `README*`
- `pyproject.toml`
- `requirements*.txt`
- `package.json`
- `tsconfig.json`
- `go.mod`
- `Cargo.toml`
- `Dockerfile`
- `docker-compose*.yml`
- `src/**/*`
- `tests/**/*`

Se ignoran directorios pesados como `.git`, `.venv`, `node_modules`, `dist`, `build`, `target`, `__pycache__`, `.next` y caches comunes.

La lectura está limitada por:

- `--max-files`, por defecto `20`.
- `--max-chars-per-file`, por defecto `12000`.

Los archivos binarios o no decodificables como UTF-8 se omiten.

## Contexto base

`--base-context` sirve para orientar a los agentes con información estable del sistema, como arquitectura, servicios, parámetros no secretos de conexión, SLAs, restricciones operativas o convenciones del proyecto.

Ejemplo de contenido útil:

- servicios involucrados
- hosts y puertos no secretos
- nombres de colas, topics o bases de datos
- rutas principales de APIs
- límites de latencia esperados
- restricciones de despliegue o rollback
- herramientas de observabilidad disponibles

Por defecto, la CLI intenta redactar valores que parecen secretos antes de enviarlos a los modelos, incluyendo claves como `password`, `token`, `secret`, `api_key` y credenciales en URIs.

Si necesitas desactivar esa redacción explícitamente:

```bash
agents-discuss "Diagnosticar conexión a base de datos" \
  --base-context local-context.md \
  --no-redact-context
```

Evita enviar credenciales reales a modelos remotos salvo que tengas una razón clara y controles de seguridad adecuados.

También puedes ejecutarlo como módulo:

```bash
python -m agents_discussion.cli "El worker se queda procesando mensajes duplicados"
```

### Web local

Puedes abrir una aplicación web local para ver la conversación de los agentes y el resultado en tiempo real:

```bash
agents-discuss-web
```

Luego abre:

```text
http://127.0.0.1:8000
```

La web permite enviar:

- tema del diagnóstico
- uno o varios archivos de contexto base
- archivo de incidente/logs
- ruta de proyecto en disco
- patrones include separados por coma
- límites de archivos y caracteres por archivo

Los eventos se reciben con Server-Sent Events desde:

```text
/api/runs/{run_id}/events
```

La interfaz muestra cada turno cuando termina:

- Diagnóstico Principal
- Revisor Escéptico
- Contrarréplica
- Moderador
- Resultado final

Nota: la ruta de proyecto se lee desde el disco de la máquina donde corre el servidor web. Úsalo como herramienta local o restringe el acceso si lo expones en red.

## Criterios de parada

- El moderador emite un estado distinto de `continue`.
- La confianza alcanza `CONFIDENCE_THRESHOLD`.
- Se alcanza `MAX_ROUNDS`.
- Faltan datos críticos.
- Existe un fix mínimo suficientemente claro o un desacuerdo estructurado.

## Salida

La salida final incluye:

- estado
- confianza
- riesgo
- hipótesis principal
- evidencia
- evidencia faltante
- hipótesis rechazadas
- siguiente paso
- fix recomendado
- validación
- motivo de cierre
