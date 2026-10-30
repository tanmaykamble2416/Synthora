# RESEARCHMIND

AI-powered research intelligence and literature review platform.

> Status: **Day 1 — backend foundation** (FastAPI + async SQLAlchemy + PostgreSQL 16/pgvector via Docker Compose).

## Prerequisites (Windows)

- Python **3.12** (`py -3.12 --version`)
- Docker Desktop (WSL 2 backend), running
- Git

## Setup (Windows PowerShell, from the repo root)

```powershell
# 1. Secrets file (never commit .env)
Copy-Item .env.example .env
# Edit .env and set a long random POSTGRES_PASSWORD

# 2. Start PostgreSQL 16 + pgvector
docker compose up -d
docker compose ps                # STATUS must show "healthy"

# 3. Verify pgvector inside the container
docker compose exec postgres psql -U researchmind -d researchmind -c "SELECT extname, extversion FROM pg_extension;"

# 4. Python environment
cd backend
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1     # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
python -m pip install --upgrade pip
pip install -r requirements-dev.txt

# 5. Run the API
uvicorn app.main:app --reload
```

## Verify

```powershell
curl.exe http://127.0.0.1:8000/api/v1/health
curl.exe http://127.0.0.1:8000/api/v1/health/db
# Swagger UI: http://127.0.0.1:8000/docs

# Tests (from backend/, venv active)
pytest                                   # unit tests, no DB needed
$env:RUN_DB_TESTS=1; pytest -m integration; Remove-Item Env:RUN_DB_TESTS
```

Failure check: `docker compose stop postgres`, then `/api/v1/health/db` must return **503** with
`DATABASE_UNAVAILABLE`, while `/api/v1/health` still returns 200. `docker compose start postgres` to recover.

## Notes

- Postgres is published on `127.0.0.1:5433` (not 5432) to avoid clashing with a native Postgres install.
- `infra/postgres/init/*.sql` only runs when the data volume is **empty**. From Day 2, Alembic migrations are the source of truth for schema and extensions.
- Pin the image after first pull: `docker image inspect pgvector/pgvector:pg16 --format "{{index .RepoDigests 0}}"` and use that digest in `docker-compose.yml`.
