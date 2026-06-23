# Technical Base Context

Describes stable information about the system to guide the agents during diagnostics.
Do not include real secrets. If you include sensitive values by mistake, the CLI attempts to redact them by default.

## Architecture

- Main service: HTTP API in Python/FastAPI.
- Async worker: processes messages from a queue.
- Database: PostgreSQL.
- Cache: Redis.
- Observability: structured JSON logs and Prometheus metrics.

## Environments

- Production: Kubernetes.
- Staging: Kubernetes with lower capacity.
- Development: Docker Compose.

## Non-Secret Parameters

- DB host: postgres.internal
- DB port: 5432
- DB name: app
- Redis host: redis.internal
- Redis port: 6379
- Queue name: orders-events
- API base path: /api/v1

## Operational Constraints

- Avoid destructive migrations during business hours.
- Prioritize reversible fixes.
- Maintain compatibility with legacy mobile clients.
- Validate changes with integration tests and p95/p99 metrics.

## Relevant Performance Signals

- Target p95 API latency: < 300 ms.
- Target p99 API latency: < 1000 ms.
- Normal CPU per pod: 35-60%.
- Normal memory per pod: < 70% of the limit.
