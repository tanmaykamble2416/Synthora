# RESEARCHMIND — PROGRESS LEDGER

## Session: Day 1 — Backend foundation (2026-10-30)

### 1. Completed tasks
- Modular FastAPI backend skeleton (app factory + lifespan).
- Typed configuration via pydantic-settings, single root `.env`, secrets as `SecretStr`.
- Async SQLAlchemy 2.0 engine (asyncpg) with pooling, pre-ping, connect timeout; per-request session dependency with rollback on error.
- Docker Compose: PostgreSQL 16 + pgvector, bound to 127.0.0.1:5433, healthcheck, named volume.
- pgvector enabled via init SQL script.
- `GET /api/v1/health` (liveness) and `GET /api/v1/health/db` (readiness: SELECT 1, server version, pgvector version, latency).
- Consistent error envelope `{"error": {code, message, details}}` for app errors, 404s, 422 validation, and unhandled 500s.
- Tests: 14/14 passing in Claude's Linux sandbox (13 unit + 1 integration against real PostgreSQL 16.15 / pgvector 0.6.0); live uvicorn boot verified (/health 200, /health/db 200). **Pending: user verification on Windows + Docker Compose (pgvector/pgvector:pg16).**

### 2. Modified files
docker-compose.yml, .env.example, .gitignore, README.md, PROGRESS.md,
infra/postgres/init/01-extensions.sql,
backend/{requirements.txt, requirements-dev.txt, pyproject.toml},
backend/app/{__init__.py, main.py},
backend/app/core/{config.py, logging.py, exceptions.py},
backend/app/db/session.py,
backend/app/schemas/{error.py, health.py},
backend/app/services/health_service.py,
backend/app/api/v1/{router.py, endpoints/health.py},
backend/tests/{conftest.py, test_config.py, test_health.py, test_db_integration.py}

### 3. Current architecture
Route (api/v1/endpoints) -> Service (services) -> DB (db/session engine/sessions).
Schemas in schemas/, config/logging/errors in core/. Engine lives on app.state, created in lifespan, disposed on shutdown.

### 4. Database schema changes
- Extension `vector` enabled (init script). No tables yet. Alembic not yet initialised.

### 5. Dependencies introduced
fastapi 0.162.0, uvicorn[standard] 0.54.0, SQLAlchemy[asyncio] 2.0.55, asyncpg 0.32.0,
pydantic 2.14.2, pydantic-settings 2.16.2; dev: pytest 9.2.0, httpx2 2.16.0.
Image: pgvector/pgvector:pg16 (pin digest after first pull).

### 6. Known bugs / risks
- `/health/db` exposes server and pgvector versions; restrict or put behind auth before any public deployment.
- Init SQL runs only on an empty volume; Alembic migration must also `CREATE EXTENSION IF NOT EXISTS vector`.
- No authentication yet (needed before document access control on Day 4).

### 7. Remaining tasks (Days 2–3)
- Alembic setup (async env.py) + first migration enabling `vector`.
- Declarative Base + models: workspaces, papers (status, server-side storage key), (chunks later).
- Repository/service layer + CRUD APIs for workspaces/papers, with pagination and duplicate handling.
- Integration tests against real Postgres using a separate test database.

### 8. Exact next development milestone
Day 2: initialise Alembic (async), create `Base`, `Workspace` model and first migration; apply and roll back successfully on the Docker Postgres.
