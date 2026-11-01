# SYNTHORA

AI-powered research intelligence and literature review platform. *Intelligence Beyond Papers.*

> Status: **Day 4: secure PDF upload and file storage** (pending local verification). Days 1–3 verified.
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

## Configuration (paper storage)

| Variable | Default | Meaning |
|---|---|---|
| `PAPER_STORAGE_DIR` | `storage` | Root directory for uploaded PDFs. A relative path is resolved against the **project root** (the folder that holds `.env`), not the current directory, so `uvicorn` and `pytest` started from `backend/` use the same place. An absolute path is used as is. |
| `MAX_UPLOAD_SIZE_MB` | `25` | Maximum size of one PDF, in MB (1 MB = 1,048,576 bytes). Allowed range: 1–512. |

`storage/` is ignored by Git, and so are `*.pdf` and `*.part` files anywhere in the repo.

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
python -m pytest

# Integration tests: ONLY touch TEST_POSTGRES_DB (default researchmind_test)
$env:RUN_DB_TESTS="1"; python -m pytest; Remove-Item Env:RUN_DB_TESTS
```

How the development database and files are kept safe from tests:
- the test DB name must end in `_test`, differ from `POSTGRES_DB` and be a plain identifier, or the run aborts;
- `researchmind_test` is created inside the existing container if missing, and no database is ever dropped;
- data is reset with `TRUNCATE`, and only on the test DB;
- the integration suite runs the migration round trip (`upgrade → downgrade → upgrade`) and `alembic check` on the test DB;
- every test that stores files gets its own temporary storage directory outside the project, and a fixture
  asserts this, so tests never write to your real `storage/`.

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
| **POST** | `/api/v1/workspaces/{id}/papers` | Bearer | **Upload a PDF** (multipart field `file`) → 201 + `Location` header |
| GET | `/api/v1/workspaces/{id}/papers` | Bearer | List papers in own workspace (`limit` ≤ 100, `offset`; newest first) |
| GET | `/api/v1/papers/{id}` | Bearer | Own paper only (others' → 404) |
| PATCH | `/api/v1/papers/{id}` | Bearer | Edit `title`, `abstract`, `authors`, `publication_year`, `doi` only |
| DELETE | `/api/v1/papers/{id}` | Bearer | Delete the paper record, its sections/chunks and its stored PDF |

Paper responses never include `storage_key` (server file location), `error_message` (raw processing
errors) or any filesystem path. Both fields stay in the database for server-side use; a failed paper is
visible to its owner as `"status": "failed"`.

Errors always look like `{"error": {"code", "message", "details"}}`.

## PDF upload

`POST /api/v1/workspaces/{workspace_id}/papers`, `multipart/form-data` with exactly one file part named `file`.
No other form fields are accepted.

**Order of checks** (the body is read only after steps 1–2 pass):

| # | Check | Failure |
|---|---|---|
| 1 | Bearer token valid | 401 `AUTHENTICATION_FAILED` |
| 2 | Workspace exists and belongs to you (SQL-scoped) | 404 `NOT_FOUND` |
| 3 | Declared `Content-Length` ≤ limit + 64 KiB multipart overhead | 413 `PAYLOAD_TOO_LARGE` |
| 4 | Body is `multipart/form-data` with one file in `file` | 415 / 400 |
| 5 | Bytes actually received ≤ limit + 64 KiB (counted, `Content-Length` not trusted) | 413 |
| 6 | Filename ends in `.pdf` (any case), after sanitising | 415 `UNSUPPORTED_MEDIA_TYPE` |
| 7 | Declared MIME is `application/pdf`, `application/octet-stream` or absent | 415 |
| 8 | Content starts with `%PDF-<digit>.<digit>` at byte 0 | 400 `BAD_REQUEST` |
| 9 | File not empty, file ≤ `MAX_UPLOAD_SIZE_MB` (counted per 1 MiB chunk) | 400 / 413 |
| 10 | Same SHA-256 not already in this workspace | 409 `CONFLICT` |

**Accepted files.** PDFs only. The extension and MIME type are only filters; the `%PDF-` signature in the
content decides. `application/octet-stream` (sent by some clients when the type is unknown) is accepted
**only** because the signature check still has to pass.

**Strict signature policy (intentional).** The header must be at byte 0, as the PDF standard (ISO 32000)
requires. Some PDF readers tolerate up to 1 KB of junk before `%PDF-`, but leading bytes are how "polyglot"
files hide a second format (HTML, ZIP, script) in front of a PDF. Academic PDFs from arXiv and publishers
start at byte 0. A file with leading bytes is rejected with 400.

**Filenames.** The original name is stored as metadata only (`original_filename`): basename only (client paths
like `C:\Users\…\x.pdf` or `../../x.pdf` are reduced to `x.pdf`), control and bidi characters removed (blocks
`invoice<U+202E>fdp.exe` spoofing), at most 255 characters. It is **never** used on disk.

**Duplicates.** SHA-256 is computed by the server while streaming (any client-sent hash is ignored).
The same PDF in the same workspace returns 409. The same PDF in a different workspace is allowed and stored
as a separate file. A fast pre-check catches most duplicates; concurrent uploads that race past it are stopped
by the database constraint `UNIQUE (workspace_id, sha256)` and also return 409.

**What is stored.** A `papers` row with `status = "uploaded"`, the real size and SHA-256, and the original
filename. `page_count`, `title`, `authors` and the other metadata stay `null` until extraction (Day 5).
Nothing is guessed.

## File storage architecture

```
<PAPER_STORAGE_DIR>/
├── papers/YYYY/MM/<uuid4>.pdf          permanent files; the storage key is this relative path
├── .tmp/<random>.part                  uploads in progress (same volume, so the final move is atomic)
└── .cleanup/pending-deletions.jsonl    files whose deletion failed, queued for retry
```

- **Server-generated keys only.** Every key must match `papers/<4 digits>/<01-12>/<uuid4>.pdf` before it
  becomes a path. Then the resolved path must still be inside the root, which also defeats symlink escapes.
  `..`, absolute paths, drive letters, backslashes, UNC paths and client-chosen names are all rejected.
- **Upload lifecycle.** Stream to `.tmp/` (hash + size + signature checked chunk by chunk, never the whole file
  in memory), duplicate pre-check, then an atomic `os.replace()` into `papers/…`, then INSERT + COMMIT.
  If anything fails before the move, the temp file is removed. If the INSERT/COMMIT fails after the move
  (including a duplicate race or a client disconnect), the new permanent file is removed again.
- **Deletion.** `DELETE /papers/{id}` deletes the row (sections and chunks cascade) and **commits first**,
  then removes the file. If removing the file fails (e.g. a Windows file lock), the API still returns 204
  with no internal details. The failure is logged, and the key is appended to
  `.cleanup/pending-deletions.jsonl`. The row is never recreated. Retry later with:

  ```powershell
  cd D:\Projects\Synthora\backend; .\.venv\Scripts\Activate.ps1
  python -c "from app.core.config import get_settings; from app.services.storage_service import StorageService; print(StorageService(get_settings().paper_storage_root).retry_pending_deletions())"
  ```

- **No file contents in PostgreSQL.** Only metadata and the key.

## Manual testing (Swagger UI)

You need one or two real PDF files (any paper), a non-PDF file (e.g. a `.txt`), and optionally a copy of a
PDF renamed to `.txt`.

### 1. Start the stack

```powershell
cd D:\Projects\Synthora
docker compose up -d; docker compose ps          # postgres must be "healthy"
cd backend; .\.venv\Scripts\Activate.ps1
alembic current                                  # expect: 0001 (head)
uvicorn app.main:app --reload                    # open http://127.0.0.1:8000/docs
```

### 2. Create a user, log in, create a workspace
1. `POST /api/v1/auth/register` with `{"email": "demo@example.com", "password": "demo-password-123"}` → 201.
2. Click **Authorize** (top right). Enter username = `demo@example.com` and password = `demo-password-123`, then click **Authorize**.
3. `POST /api/v1/workspaces` with `{"name": "Demo Survey"}` → 201. Copy the `id`.

### 3. Upload
In `POST /api/v1/workspaces/{workspace_id}/papers`, click **Try it out**, paste the workspace id, choose a file for `file`, then click **Execute**.

| Step | What to send | Expected |
|---|---|---|
| Upload a real PDF | `paper.pdf` | 201. `status: "uploaded"`, `page_count: null`, correct `file_size_bytes`, a 64-char `sha256`. **No** `storage_key` / `error_message`. A new file appears under `D:\Projects\Synthora\storage\papers\YYYY\MM\<uuid>.pdf` |
| Same PDF again | same file | 409 `CONFLICT` |
| Same PDF, other workspace | create a 2nd workspace, upload there | 201 |
| Non-PDF | `notes.txt` | 415 `UNSUPPORTED_MEDIA_TYPE` |
| Fake PDF | a text file renamed to `fake.pdf` | 400 `BAD_REQUEST` ("does not start with a %PDF- header") |
| Empty | an empty file named `empty.pdf` | 400 |
| Too big | set `MAX_UPLOAD_SIZE_MB=1` in `.env`, restart uvicorn, upload a PDF over 1 MB | 413 `PAYLOAD_TOO_LARGE` (set it back afterwards) |
| No token | **Authorize → Logout**, upload | 401 |

### 4. Read, update, delete
| Step | Request | Expected |
|---|---|---|
| List | `GET /api/v1/workspaces/{id}/papers` | 200, newest first, `total` correct, no internal fields |
| Get | `GET /api/v1/papers/{paper_id}` | 200 |
| Update | `PATCH /api/v1/papers/{paper_id}` with `{"publication_year": 2017, "doi": "https://doi.org/10.48550/arXiv.1706.03762"}` | 200, `doi` normalised to `10.48550/arXiv.1706.03762` |
| Internal field | `PATCH` with `{"status": "processed"}` or `{"storage_key": "x"}` | 422 (whole request rejected) |
| Delete | `DELETE /api/v1/papers/{paper_id}` | 204. Then `GET` → 404, and the file under `storage\papers\…` is gone |

### 5. Cross-user isolation
Log out, register `other@example.com` and authorize as that user. Then:
- upload into the demo user's workspace;
- `GET`, `PATCH` and `DELETE` one of the demo user's papers.

All must return **404**, and the demo user's file must still be on disk.

### 6. Clean up
Delete demo papers with `DELETE /api/v1/papers/{id}`, which removes the files too. Rows seeded by hand
during Day 3 testing (keys starting `manual/`) have no file. Deleting them works and logs
"invalid storage key; nothing was deleted" on the server.

## Notes

- Postgres is published on `127.0.0.1:5433` (not 5432) to avoid clashing with a native Postgres install.
- `infra/postgres/init/*.sql` only runs when the data volume is empty. Alembic is the source of truth for the schema.
- Never run `docker compose down -v`: `-v` deletes the database volume.
- Pin the image after first pull: `docker image inspect pgvector/pgvector:pg16 --format "{{index .RepoDigests 0}}"`.
