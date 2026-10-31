# SYNTHORA

AI-powered research intelligence and literature review platform.

> Status: **Day 2: database architecture, migrations and JWT auth** (pending local verification).
> Formerly RESEARCHMIND. Only the branding changed: the Docker Compose project, container,
> database name, database user and volume keep the `researchmind` names on purpose
> (renaming the Compose project would create a new, empty volume).

## Prerequisites (Windows)

- Python **3.12** (`py -3.12 --version`)
- Docker Desktop (WSL 2 backend), running
- Git

## First-time setup (Windows PowerShell, from the repo root)

```powershell
# 1. Secrets file (never commit .env)
Copy-Item .env.example .env
# Edit .env: set POSTGRES_PASSWORD and JWT_SECRET_KEY (see "Secrets" below)

# 2. Start PostgreSQL 16 + pgvector
docker compose up -d
docker compose ps                # STATUS must show "healthy"

# 3. Python environment
cd backend
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1     # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
python -m pip install --upgrade pip
pip install -r requirements-dev.txt

# 4. Apply database migrations
alembic upgrade head

# 5. Run the API
uvicorn app.main:app --reload    # Swagger UI: http://127.0.0.1:8000/docs
```

## Secrets

`JWT_SECRET_KEY` is required. The app refuses to start while it is missing, too short
(at least 32/48/64 bytes for HS256/HS384/HS512) or still the `.env.example` placeholder. Generate one with:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

## Database migrations (Alembic)

Run these from `backend/`. Alembic reads the connection settings from the root `.env`; there are no credentials in `alembic.ini`.

```powershell
alembic current            # which revision the DB is at
alembic upgrade head       # apply all migrations
alembic check              # fails if the models and migrations have drifted
alembic upgrade head --sql # print the SQL without connecting
```

`alembic downgrade base` **drops every application table and its data**. Use it on the dev DB
only while it holds nothing you need. Downgrade never drops the `vector` extension.

## Tests

```powershell
# Unit tests: no database needed
pytest

# Integration tests: ONLY touch TEST_POSTGRES_DB (default researchmind_test)
$env:RUN_DB_TESTS=1; pytest; Remove-Item Env:RUN_DB_TESTS
```

How the development database is kept safe from tests:
- the test DB name must end in `_test`, differ from `POSTGRES_DB` and be a plain identifier, or the run aborts;
- `researchmind_test` is created inside the existing container if missing, and no database is ever dropped;
- data is reset with `TRUNCATE`, and only on the test DB;
- the integration suite runs the migration round trip (`upgrade → downgrade → upgrade`) and `alembic check` on the test DB.

## API (v1)

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/v1/health` | – | Liveness |
| GET | `/api/v1/health/db` | – | Readiness (DB + pgvector) |
| POST | `/api/v1/auth/register` | – | Create account |
| POST | `/api/v1/auth/login` | – | OAuth2 password form (`username` = email) → bearer token |
| GET / PATCH / DELETE | `/api/v1/users/me` | Bearer | Own account only |
| POST / GET | `/api/v1/workspaces` | Bearer | Create / list own workspaces (`limit`, `offset`) |
| GET / PATCH / DELETE | `/api/v1/workspaces/{id}` | Bearer | Own workspace only (others' → 404) |

Errors always look like `{"error": {"code", "message", "details"}}`.

## Notes

- Postgres is published on `127.0.0.1:5433` (not 5432) to avoid clashing with a native Postgres install.
- `infra/postgres/init/*.sql` only runs when the data volume is empty. Alembic is the source of truth for the schema.
- Never run `docker compose down -v`: `-v` deletes the database volume.
- Pin the image after first pull: `docker image inspect pgvector/pgvector:pg16 --format "{{index .RepoDigests 0}}"`.
