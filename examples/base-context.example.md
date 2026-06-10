# Contexto Base Técnico

Describe información estable del sistema para orientar a los agentes durante diagnósticos.
No incluyas secretos reales. Si incluyes valores sensibles por error, la CLI intenta redactarlos por defecto.

## Arquitectura

- Servicio principal: API HTTP en Python/FastAPI.
- Worker asíncrono: procesa mensajes desde una cola.
- Base de datos: PostgreSQL.
- Caché: Redis.
- Observabilidad: logs estructurados JSON y métricas Prometheus.

## Entornos

- Producción: Kubernetes.
- Staging: Kubernetes con menor capacidad.
- Desarrollo: Docker Compose.

## Parámetros No Secretos

- DB host: postgres.internal
- DB port: 5432
- DB name: app
- Redis host: redis.internal
- Redis port: 6379
- Queue name: orders-events
- API base path: /api/v1

## Restricciones Operativas

- Evitar migraciones destructivas durante horario laboral.
- Priorizar fixes reversibles.
- Mantener compatibilidad con clientes móviles antiguos.
- Validar cambios con tests de integración y métricas p95/p99.

## Señales De Rendimiento Relevantes

- Latencia objetivo p95 API: < 300 ms.
- Latencia objetivo p99 API: < 1000 ms.
- CPU normal por pod: 35-60%.
- Memoria normal por pod: < 70% del límite.
