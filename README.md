# SYNTHORA

AI-powered research intelligence and literature review platform. *Intelligence Beyond Papers.*

> Status: **Day 5: PDF text extraction** (pending local verification). Days 1–4 verified.
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

## Configuration (paper storage and extraction)

| Variable | Default | Meaning |
|---|---|---|
| `PAPER_STORAGE_DIR` | `storage` | Root directory for uploaded PDFs. A relative path is resolved against the **project root** (the folder that holds `.env`), not the current directory, so `uvicorn` and `pytest` started from `backend/` use the same place. An absolute path is used as is. |
| `MAX_UPLOAD_SIZE_MB` | `25` | Maximum size of one PDF, in MB (1 MB = 1,048,576 bytes). Allowed range: 1–512. |
| `EXTRACTION_MAX_PAGES` | `2000` | PDFs with more pages fail with `too_many_pages`. Allowed range: 1–20000. |
| `EXTRACTION_STALE_MINUTES` | `15` | A paper left in `processing` longer than this (e.g. after a server crash) can be extracted again. |

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
| DELETE | `/api/v1/papers/{id}` | Bearer | Delete the paper record, its pages/sections/chunks and its stored PDF |
| **POST** | `/api/v1/papers/{id}/extract` | Bearer | **Extract the text of every page** → 200 `ExtractionSummary` (409 if already running) |
| GET | `/api/v1/papers/{id}/pages` | Bearer | Extracted pages in page order (`limit` ≤ 50, `offset`) |
| GET | `/api/v1/papers/{id}/pages/{page_number}` | Bearer | One extracted page (1-based) |

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

## PDF text extraction

`POST /api/v1/papers/{paper_id}/extract` reads the stored PDF with **PyMuPDF** and saves the text of
**every page** as its own row in `paper_pages`, numbered from 1 exactly as in the PDF. Extraction is manual
and synchronous for now (automatic background processing arrives with Celery on Day 10).

### Workflow

```
POST /papers/{id}/extract
  ├─ T1 claim     UPDATE papers SET status='processing' WHERE id, owned, claimable RETURNING storage_key
  │               + delete old pages, sections and chunks                                   COMMIT
  ├─ extract      StorageService.resolve(storage_key) → pymupdf, page by page   (thread pool, NO transaction open)
  │               clean text → classify page → PageExtraction(page_number, text, counts, status, needs_ocr)
  └─ T2 persist   SELECT … FOR UPDATE; still 'processing'? → one batched INSERT of all pages
                  + status='processed', page_count, processed_at                            COMMIT
     or T2 fail   status='failed', failure_code, internal error_message                    COMMIT
```

- **Short transactions.** T1 and T2 take milliseconds. The PDF work happens between them, with no
  database transaction or row lock held.
- **Atomic pages.** All pages of an extraction are written in one transaction or not at all. A failed or
  interrupted extraction never leaves old or partial pages behind: pages exist only while the paper's
  latest extraction has completed (`processed`).
- **Re-extraction** replaces every page. `UNIQUE (paper_id, page_number)` makes duplicate pages impossible,
  and derived `paper_sections` / `paper_chunks` are cleared because they would be stale.
- **One extraction per paper at a time.** The T1 conditional UPDATE acts as the lock, and a second
  request gets **409**. A claim older than `EXTRACTION_STALE_MINUTES` (judged by `papers.updated_at`, which
  the claim sets to the database's `now()`) can be taken over after a crash.

### Processing lifecycle

```
uploaded ──extract──▶ processing ──▶ processed     (pages saved; some may be flagged needs_ocr)
                          │
                          └────────▶ failed        (failure_code set; no pages)
processed / failed ──extract again──▶ processing …
```

### What a page looks like

| Field | Meaning |
|---|---|
| `page_number` | 1-based, exactly as in the PDF. Blank pages are kept, so numbers never shift. |
| `text` | Extracted text after conservative cleaning (see below). `""` if the page has no text layer. |
| `char_count`, `word_count` | Of the stored text. |
| `extraction_status` | `text` (≥ 50 non-space characters), `low_text` (1–49) or `empty` (0). |
| `needs_ocr` | `true` when the page has images but not enough text, i.e. it is probably scanned. **No OCR is performed.** |

A PDF that is **entirely scanned** is still `processed`: every page is saved with `text = ""` and
`needs_ocr = true`. Nothing is invented.

### Handled failures (HTTP 200, `status: "failed"`)

| `failure_code` | Cause |
|---|---|
| `file_missing` | The stored file is gone, or the row's storage key is not a server-generated key (e.g. hand-seeded rows) |
| `pdf_corrupted` | PyMuPDF cannot open the file, or it has no pages |
| `pdf_encrypted` | The PDF needs a password to open. Owner-password-only PDFs (permission restrictions) open and are extracted. |
| `too_many_pages` | More than `EXTRACTION_MAX_PAGES` pages |
| `extraction_error` | **Any** page could not be read. The whole extraction fails rather than presenting partial content as complete. |

`no_extractable_text` is a reserved code; it is not used today (scanned PDFs are `processed`).
The technical detail goes to the server log and the internal `papers.error_message` column, never to the
client. Unexpected errors (bugs) return the generic **500** and also mark the paper `failed` /
`extraction_error`, so it is never left stuck in `processing`.

### Text cleaning (conservative)

Removed or normalised:
- NUL (MuPDF's marker for an unmappable glyph) and other control characters;
- soft hyphens, zero-width spaces and BOMs;
- CRLF/CR, which become LF;
- runs of spaces and tabs (collapsed), and spaces at line ends;
- three or more blank lines, which become one blank line, so paragraph breaks survive;
- Unicode, normalised to NFC.

Ligatures (`ﬁ` → `fi`) are expanded by MuPDF itself.

**Never** done: NFKC (it would turn `x²` into `x2` and `ℝ` into `R`), de-hyphenation, joining lines,
reflowing, spell-checking or any rewriting. Numbers, percentages, citation markers like `[12]` and maths
symbols are kept byte for byte.

### Extraction limitations

- **No OCR.** Scanned pages are detected and flagged `needs_ocr`; their text stays empty.
- **Reading order** follows the PDF content stream, which is usually correct for LaTeX two-column papers
  but not guaranteed for every PDF. Position-sorting is deliberately not used, because it interleaves columns.
- **Tables** keep every cell and number, one cell per line, but lose their grid layout.
- **Maths** is only as good as the PDF's text layer. Glyphs without a Unicode mapping are dropped (MuPDF reports
  them as NUL), never guessed.
- **One extraction at a time per server process.** PyMuPDF is not thread-safe, so extractions are serialised
  by a process-wide lock. Day 10 moves extraction to Celery worker processes.
- The request waits until extraction finishes. That's fine for typical papers (well under a second in tests),
  but slow for very large documents until Day 10.
- If the client disconnects mid-extraction, the paper can stay `processing` until `EXTRACTION_STALE_MINUTES`
  has passed, after which it can be re-extracted.

### Licence: PyMuPDF is AGPL-3.0

PyMuPDF (`PyMuPDF==1.28.2`, imported as `pymupdf`) is dual-licensed: **GNU AGPL-3.0** or a commercial
licence from Artifex. Using it in this open portfolio project is fine. **Distributing or offering SYNTHORA as
a network service commercially** requires either complying with the AGPL (publishing the complete source
of the service) or buying an Artifex licence.

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
PDF renamed to `.txt`. For extraction checks, a scanned PDF (image-only) and a password-protected PDF are useful too.

### 1. Start the stack

```powershell
cd D:\Projects\Synthora
docker compose up -d; docker compose ps          # postgres must be "healthy"
cd backend; .\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt              # Day 5 adds PyMuPDF
alembic upgrade head                             # applies 0002 (paper_pages, failure_code)
alembic current                                  # expect: 0002 (head)
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

### 5. Extract text
| Step | Request | Expected |
|---|---|---|
| Extract | `POST /api/v1/papers/{paper_id}/extract` | 200, `status: "processed"`, `page_count` = the PDF's page count, sensible `total_words` |
| Pages | `GET /api/v1/papers/{paper_id}/pages` | 200, pages `1, 2, 3, …` in order, text matches what you see in a PDF viewer |
| One page | `GET /api/v1/papers/{paper_id}/pages/1` | 200. Page `0` → 422; a page beyond the end → 404 |
| Paper | `GET /api/v1/papers/{paper_id}` | `status: "processed"`, `page_count` filled, `failure_code: null` |
| Again | `POST …/extract` a second time | 200; `GET …/pages` still shows each page exactly once |
| Scanned PDF | upload a scanned PDF (no text layer), extract | 200 `processed`, `needs_ocr_pages` = `page_count`, empty texts |
| Encrypted PDF | upload a PDF with an open password, extract | 200 `status: "failed"`, `failure_code: "pdf_encrypted"`; `GET …/pages` → empty |

### 6. Cross-user isolation
Log out, register `other@example.com` and authorize as that user. Then:
- upload into the demo user's workspace;
- `GET`, `PATCH` and `DELETE` one of the demo user's papers;
- `POST …/extract`, `GET …/pages` and `GET …/pages/1` on one of the demo user's papers.

All must return **404**, and the demo user's file must still be on disk.

### 7. Clean up
Delete demo papers with `DELETE /api/v1/papers/{id}`, which removes the files and the extracted pages too. Rows seeded by hand
during Day 3 testing (keys starting `manual/`) have no file. Deleting them works and logs
"invalid storage key; nothing was deleted" on the server.

## Notes

- Postgres is published on `127.0.0.1:5433` (not 5432) to avoid clashing with a native Postgres install.
- `infra/postgres/init/*.sql` only runs when the data volume is empty. Alembic is the source of truth for the schema.
- Never run `docker compose down -v`: `-v` deletes the database volume.
- Pin the image after first pull: `docker image inspect pgvector/pgvector:pg16 --format "{{index .RepoDigests 0}}"`.
