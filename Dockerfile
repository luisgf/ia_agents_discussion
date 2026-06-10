FROM python:3.12-slim

# Optional clients used by diagnostic tools:
#   git (git_recent_changes), openssh (run_ssh_command host keys),
#   postgresql-client (run_db_explain). kubectl is intentionally not
#   bundled — mount it or extend this image if you need run_kubectl.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl git openssh-client postgresql-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# Persisted state (runs, audit log, custom prompt templates) lives in /data.
ENV WEB_HOST=0.0.0.0 \
    WEB_PORT=8000 \
    DATA_DIR=/data/runs \
    PROMPTS_DIR=/data/prompts

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD curl -fsS http://127.0.0.1:8000/api/settings > /dev/null || exit 1

CMD ["agents-discuss-web"]
