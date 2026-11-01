# SYNTHORA

AI-powered research intelligence and literature review platform.

> Status: **Day 3: paper management API** (pending local verification). Day 2 verified.
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
| GET | `/api/v1/workspaces/{id}/papers` | Bearer | List papers in own workspace (`limit` ≤ 100, `offset`; newest first) |
| GET | `/api/v1/papers/{id}` | Bearer | Own paper only (others' → 404) |
| PATCH | `/api/v1/papers/{id}` | Bearer | Edit `title`, `abstract`, `authors`, `publication_year`, `doi` only |
| DELETE | `/api/v1/papers/{id}` | Bearer | Deletes the DB record (stored PDF removal arrives with the Day 4/5 storage service) |

There is no paper upload endpoint yet (Day 4). Until then, exercise the paper endpoints with
demo rows (see "Manual testing" below).

Paper responses never include `storage_key` (server file location) or `error_message`
(raw processing errors). Both stay in the database for server-side use; a failed paper is
visible to its owner as `"status": "failed"`.

Errors always look like `{"error": {"code", "message", "details"}}`.

## Manual testing (Swagger UI)

### 1. Start the stack

```powershell
cd D:\Projects\Synthora
docker compose up -d; docker compose ps          # postgres must be "healthy"
cd backend; .\.venv\Scripts\Activate.ps1
alembic current                                  # expect: 0001 (head)
uvicorn app.main:app --reload                    # open http://127.0.0.1:8000/docs
```

### 2. Create a user and log in
1. `POST /api/v1/auth/register` with `{"email": "demo@example.com", "password": "demo-password-123"}` → 201.
2. Click **Authorize** (top right). Enter username = `demo@example.com` and password = `demo-password-123`, then click **Authorize**. Swagger now sends the bearer token on every request.
3. `GET /api/v1/users/me` → 200 with your account.

### 3. Create a workspace
`POST /api/v1/workspaces` with `{"name": "Demo Survey"}` → 201. Copy the returned `id`.

### 4. Seed demo papers (until the Day 4 upload endpoint exists)
This **adds rows to the development database**. It deletes nothing, and it can be re-run
(it generates unique keys; each run adds 3 more rows, and the table below assumes one run).
In a **second** PowerShell window, from the repo root:

```powershell
$ws = "PASTE-WORKSPACE-ID-HERE"
docker compose exec postgres psql -U researchmind -d researchmind -c "INSERT INTO papers (workspace_id, original_filename, storage_key, sha256, file_size_bytes, title, status, error_message, created_at) SELECT '$ws', v.fname, 'manual/' || gen_random_uuid() || '.pdf', encode(sha256(gen_random_uuid()::text::bytea), 'hex'), 1024, v.title, v.status, v.err, now() - v.age FROM (VALUES ('attention.pdf', 'Attention Is All You Need', 'uploaded', NULL, interval '2 minutes'), ('bert.pdf', 'BERT', 'uploaded', NULL, interval '1 minute'), ('broken.pdf', 'Broken upload', 'failed', 'Traceback: /srv/internal/path.pdf', interval '0 minutes')) AS v(fname, title, status, err, age) RETURNING id, title, status;"
```

### 5. Exercise the paper endpoints

| Step | Request | Expected |
|---|---|---|
| List | `GET /api/v1/workspaces/{id}/papers` | 200: 3 items, newest first (`Broken upload`, `BERT`, `Attention…`), `total: 3`. **No** `storage_key` / `error_message` keys; the broken one shows `"status": "failed"` |
| Paginate | same, `limit=2&offset=2` | 200: 1 item, `total: 3` |
| Bad pagination | same, `limit=101` or `offset=-1` | 422 `VALIDATION_ERROR` |
| Get | `GET /api/v1/papers/{paper_id}` | 200 |
| Update | `PATCH /api/v1/papers/{paper_id}` with `{"publication_year": 2017, "doi": "https://doi.org/10.48550/arXiv.1706.03762", "authors": [{"name": "Ashish Vaswani", "affiliation": "Google Brain"}]}` | 200, `doi` normalised to `10.48550/arXiv.1706.03762` |
| Clear | `PATCH` with `{"doi": null}` | 200, `doi: null` |
| Internal field | `PATCH` with `{"status": "processed"}` or `{"storage_key": "x"}` | 422 (whole request rejected) |
| Empty body | `PATCH` with `{}` | 422 |
| Bad year | `PATCH` with `{"publication_year": 1700}` | 422 |
| Delete | `DELETE /api/v1/papers/{paper_id}` | 204, then `GET` on it → 404 |
| Bad id | `GET /api/v1/papers/not-a-uuid` | 422 |
| No token | Click **Authorize → Logout**, then `GET /api/v1/papers/{paper_id}` | 401 |

### 6. Check cross-user isolation
Log out, register `other@example.com`, and authorize as that user. Then `GET`, `PATCH` and `DELETE`
on one of the demo user's `paper_id`s, and `GET /api/v1/workspaces/{demo-workspace-id}/papers`.
All must return **404** with the same body as a paper that doesn't exist. Log back in as the demo user
and confirm nothing changed.

### 7. Clean up the demo rows (optional)
Use `DELETE /api/v1/papers/{id}` in Swagger, or remove only the seeded rows:

```powershell
docker compose exec postgres psql -U researchmind -d researchmind -c "DELETE FROM papers WHERE storage_key LIKE 'manual/%' RETURNING id, title;"
```

## Notes

- Postgres is published on `127.0.0.1:5433` (not 5432) to avoid clashing with a native Postgres install.
- `infra/postgres/init/*.sql` only runs when the data volume is empty. Alembic is the source of truth for the schema.
- Never run `docker compose down -v`: `-v` deletes the database volume.
- Pin the image after first pull: `docker image inspect pgvector/pgvector:pg16 --format "{{index .RepoDigests 0}}"`.
