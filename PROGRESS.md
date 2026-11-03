# SYNTHORA (formerly RESEARCHMIND): PROGRESS LEDGER

Tagline: Intelligence Beyond Papers.

---

## Session: Day 5: PDF text extraction engine (2026-11-02)

**Status: implemented and verified in Claude's Linux sandbox (PostgreSQL 16.15, PyMuPDF 1.28.2).
PENDING: local verification on Windows + Docker Compose. Day 5 is not complete until that passes. Not committed.**

### 1. Completed tasks
- **Endpoints (owner-only, OpenAPI-documented):**
  - `POST /api/v1/papers/{id}/extract`: returns 200 `ExtractionSummary` for every handled outcome (`processed`, or `failed` + `failure_code`); 409 if already running; 404 if not yours;
  - `GET /api/v1/papers/{id}/pages` (paginated, limit ≤ 50);
  - `GET /api/v1/papers/{id}/pages/{page_number}` (1-based).
- **Extraction engine** (`pdf_extraction.py`): pure, synchronous, no DB or FastAPI imports, so it's reusable by a Day 10 Celery worker. It:
  - opens the PDF with `with pymupdf.open(...)` (always closed) and enforces `EXTRACTION_MAX_PAGES`;
  - extracts every page in content-stream order (not `sort=True`, which interleaves columns), 1-based, with ligatures expanded;
  - classifies each page `text` / `low_text` / `empty` and sets `needs_ocr` when there are images but little text.
- **Failure handling:**
  - `file_missing`, `pdf_corrupted`, `pdf_encrypted` (user password; owner-only PDFs are extracted), `too_many_pages`;
  - `extraction_error` when **any** page raises or is unreadable (MuPDF warnings + no text). The whole document fails; nothing partial is stored.
- **Fully scanned PDF:** `processed`, all pages saved with `needs_ocr = true` (per the approved decision). No OCR.
- **Text cleaner** (`pdf_text_cleaner.py`): conservative.
  - Removes NUL/control characters, soft hyphens, ZWSP and BOM.
  - Normalises line endings to LF, collapses whitespace runs and limits blank lines.
  - Applies NFC.
  - Explicitly **no** NFKC, de-hyphenation or rewriting.
- **Orchestration** (`extraction_service.py`): three short transactions, with the PDF work outside any transaction:
  - **T1** claim (conditional UPDATE = lock) + delete old pages/sections/chunks;
  - **T2** `SELECT … FOR UPDATE` + one batched INSERT + mark processed;
  - or **T2** mark failed.
  - Unexpected errors → best-effort `failed`/`extraction_error`, then the generic 500.
- **Thread safety:** PyMuPDF is not thread-safe and its warning store is process-global, so extraction is serialised by a process-wide lock and runs in the thread pool (the event loop is not blocked).
- **`updated_at` verified** for stale recovery:
  - the claim's UPDATE sets it to the DB `now()` (ORM `onupdate`), and a test proves it;
  - the cutoff is computed in SQL (`now() - interval`), so app/DB clock skew doesn't matter.
  - Caveat: a metadata PATCH during processing also bumps `updated_at`, which can only *delay* recovery, never cause an early takeover.
- **Superseded results** (a stale takeover finished first) are discarded with 409, never written.
- `PaperRead` now includes `failure_code`.

### 2. Files
**New:**
- backend/alembic/versions/0002_paper_pages_and_failure_code.py
- backend/app/models/paper_page.py
- backend/app/repositories/paper_page_repository.py
- backend/app/services/pdf_text_cleaner.py
- backend/app/services/pdf_extraction.py
- backend/app/services/extraction_service.py
- backend/app/schemas/extraction.py
- backend/app/api/v1/endpoints/extraction.py
- backend/tests/pdf_factory.py (generates test PDFs in memory; no binary fixtures)
- backend/tests/unit/test_pdf_text_cleaner.py
- backend/tests/unit/test_pdf_extraction.py
- backend/tests/integration/test_paper_extraction_api.py

**Modified:**
- backend/app/models/paper.py (`failure_code` + 2 CHECKs, `pages` relationship)
- backend/app/models/__init__.py
- backend/app/repositories/paper_repository.py:
  - `owned_paper_ids`, `claim_for_extraction`, `delete_extracted_content`;
  - `lock_status_for_update`, `mark_processed`, `mark_failed`;
  - every one owner-scoped.
- backend/app/schemas/paper.py (`failure_code` in `PaperRead`)
- backend/app/api/v1/router.py
- backend/app/core/config.py (`EXTRACTION_MAX_PAGES`, `EXTRACTION_STALE_MINUTES`)
- backend/requirements.txt (`PyMuPDF==1.28.2`)
- backend/tests/integration/conftest.py (`paper_pages` added to the TRUNCATE list)
- backend/tests/integration/test_migrations.py (head is now `0002`; added a 0002 step-downgrade test and a constraint test)
- .env.example, README.md, PROGRESS.md

### 3. Architecture
```
endpoints/extraction.py ─▶ ExtractionService ─┬─▶ PaperRepository      (claim / lock / mark, owner-scoped SQL)
                                              ├─▶ PaperPageRepository  (batched insert, owner-scoped reads)
                                              ├─▶ StorageService.resolve(storage_key)
                                              └─▶ pdf_extraction.extract_pdf  ─▶ pdf_text_cleaner
                                                   (thread pool, process-wide MuPDF lock, no DB)
```

### 4. Database changes: migration `0002_paper_pages_and_failure_code`
- **New table `paper_pages`:**
  - columns: `paper_id` (FK → papers **ON DELETE CASCADE**), `page_number` (`CHECK ≥ 1`), `text` (NOT NULL, may be `''`), `char_count`/`word_count` (`CHECK ≥ 0`), `extraction_status` (`CHECK IN ('text','low_text','empty')`), `needs_ocr` (bool), timestamps;
  - **`UNIQUE (paper_id, page_number)`**;
  - ownership is inherited via papers → workspaces (like `paper_sections`).
- **New column `papers.failure_code`** (VARCHAR(40), nullable), with two CHECKs:
  - `failure_code_valid`: `file_missing`, `pdf_corrupted`, `pdf_encrypted`, `too_many_pages`, `no_extractable_text` (reserved), `extraction_error`;
  - `failure_code_only_when_failed`: a code is only allowed when `status = 'failed'`.
  - These CHECKs were **added by hand**: Alembic autogenerate does not detect new CHECK constraints on an existing table.
- `paper_sections` and `paper_chunks` are **unchanged and not used** for page text. They are separate, overlapping divisions for Day 6–7.
- **Verified in the sandbox**, on DBs that already held Day 4 rows:
  - upgrade, then `alembic check` clean;
  - the CHECK is enforced;
  - downgrade to `0001` removes only the new objects, and existing papers survive;
  - re-upgrade works.
  - The Day 7 embeddings migration becomes `0003`.

### 5. Dependencies introduced
- **`PyMuPDF==1.28.2`** (no transitive dependencies; abi3 wheel for Windows x64 / Python 3.12 confirmed).
  - **AGPL-3.0 or Artifex commercial**, documented in the README.
  - 2.0 was not chosen: it's a two-week-old major release with no fixes we need.

### 6. Known limitations
- No OCR: scanned pages are flagged `needs_ocr` with empty text.
- Reading order is the PDF content-stream order: right for typical LaTeX two-column papers, not guaranteed for all PDFs.
- Tables keep cells and numbers (one per line), not their grid.
- Maths is only as good as the PDF's text layer; unmappable glyphs (MuPDF NUL) are dropped, never guessed.
- One extraction at a time per process (PyMuPDF thread-safety lock); requests wait for completion. Day 10 moves this to Celery processes.
- A client disconnect mid-extraction can leave `processing` until `EXTRACTION_STALE_MINUTES` has passed; then it's reclaimable.
- Stale-recovery edge case: if a slow original run *fails* after a takeover started, the paper ends `failed` even if the takeover would have succeeded. The user re-runs extraction. A dedicated claim token would remove this; it's not worth a column today.
- `low_text` + images is flagged `needs_ocr`, which can include a figure page with a short caption (a "may need OCR" heuristic).
- Rows seeded by hand during Day 3 (`manual/…` keys) extract as `file_missing`.

### 7. Test results (Claude's sandbox; NOT yet your machine)
Run in a **fresh** virtualenv installed from `requirements-dev.txt`:
- `python -m compileall app`: OK.
- `python -m pytest` (no DB): **213 passed, 246 skipped**.
- `RUN_DB_TESTS=1 python -m pytest`: **459 passed, 0 failed**. That's the Day 4 baseline of 369, plus 52 unit tests, 36 extraction integration tests and 2 new migration tests.
- `alembic check`: "No new upgrade operations detected."; head = `0002`.
- The real `storage/` directory was never created; the dev DB sentinel was unchanged; no MuPDF messages leaked to stderr.
- **Mutation checks**: 12 safety mechanisms, each removed in turn; every removal made at least one test fail. They were:
  - unreadable-page detection, ligature expansion, the encryption check;
  - stale-page deletion on claim, the superseded-result guard, rollback on persist failure;
  - the owner filter on claim, page listing and single page;
  - the stale cutoff, and `updated_at` refresh on claim.
  - This exposed a gap (the page-listing owner filter was only covered via the service layer), now closed with a repository-level test.
- **Live uvicorn smoke test:**
  - upload → extract (200 processed, 4 pages, 1 `needs_ocr`) → page read;
  - the log contained no extracted text, storage path or token.

### 7a. Windows verification, run 1 (2026-11-02)
- **Result:** 458 passed, **1 error**, in **29 min 18 s**. The error was in the setup of `test_claim_refreshes_updated_at`: the `clean_tables` fixture's DB connection hit asyncpg's 60 s connect timeout. All 25 Day 5 files on the user's machine were checksum-identical to the sandbox-tested versions, so this is not a code difference.
- **Diagnosis:** `.env` had `POSTGRES_HOST=localhost`, but docker-compose publishes PostgreSQL on IPv4 `127.0.0.1` only.
  - On Windows, `localhost` resolves to IPv6 `::1` first, and each refused `::1` attempt costs ~2 s before the `127.0.0.1` fallback.
  - The suite opens ~900 short-lived connections: ~900 × 2 s ≈ 30 min, which matches the run time.
  - The single 60 s timeout is most likely a transient stall of Docker Desktop's port forwarding during that connection churn. It's not reproducible in the sandbox and should be re-checked after the host fix.
- **Security finding:** pytest's default *long* traceback printed the arguments of `asyncpg.connect`, **including the PostgreSQL password**. Reproduced with a canary password: long traceback = leaked; `--tb=short`/`native`/`line` = not leaked.
- **Fixes:**
  - `backend/pyproject.toml`: `addopts = "-ra --tb=short"`, verified to print neither the DB password nor the JWT secret;
  - default `postgres_host` and `.env.example` changed to `127.0.0.1`;
  - user actions: set `POSTGRES_HOST=127.0.0.1` in `.env` and rotate the exposed DB password (`ALTER ROLE`, then update `.env`).
- Sandbox re-run after the fixes: 213 passed / 246 skipped (no DB); **459 passed** (with DB); `alembic check` clean.

### 8. Remaining tasks
- Local verification of Day 5, run 2 (after the host fix and password rotation), then review and commit.
- Day 6: structured extraction (title, authors, abstract, sections via GROBID; methodology, datasets and results) into `papers` metadata and `paper_sections`, with page-anchored evidence.
- Day 7: migration 0003 (embeddings) + hybrid retrieval (pgvector + `rank_bm25`) over page-anchored chunks.
- Day 10: Celery runs extraction automatically after upload; schedule `retry_pending_deletions()` and orphan reconciliation.

### 9. Exact next development milestone
On Windows, from `backend/`:
- `pip install -r requirements-dev.txt`;
- `alembic upgrade head` (expect `0002`);
- `python -m compileall app`;
- `python -m pytest` (expect 213 passed, 246 skipped; one more skip if symlinks are unavailable);
- `$env:RUN_DB_TESTS="1"; python -m pytest` (expect 459 passed);
- `alembic check`;
- the README Swagger "Extract text" steps with a real paper.

Then review and commit. Then Day 6.

---

## Session: Day 4: Secure PDF upload and file storage (2026-11-01)

**Status: verified locally by the user on Windows (369 tests passed) and committed (`cd7d634`).**

### 1. Completed tasks
- `POST /api/v1/workspaces/{workspace_id}/papers` (multipart field `file`): returns 201 `PaperRead` and a `Location` header.
- **Auth and ownership before the body is read.** There's no FastAPI `File()` parameter (that would read the whole body before the dependencies run). The endpoint checks the token, then workspace ownership (SQL, 404), and only then parses the multipart body.
- **Two-layer size limit**, `MAX_UPLOAD_SIZE_MB` (default 25, MiB):
  - the request layer rejects a declared `Content-Length` above the limit + 64 KiB, and also counts the bytes actually received through a wrapped ASGI `receive`, so a missing or false `Content-Length` can't bypass it;
  - the file layer enforces the exact limit chunk by chunk (1 MiB) while copying into storage.
- **Validation:**
  - filename sanitised (basename, NFC, control/bidi characters stripped, ≤ 255) and must end in `.pdf`, otherwise 415;
  - declared MIME must be `application/pdf`, `application/octet-stream` or absent, otherwise 415;
  - the content must start with `%PDF-d.d` **at byte 0** (strict, intentional: anti-polyglot), otherwise 400, checked before the first byte is written;
  - empty file → 400;
  - malformed multipart, a wrong field, two files or extra fields → 400. Starlette's parser messages are not echoed.
- **SHA-256** computed incrementally by the server.
- **Duplicates:** a pre-check gives 409, and the `UNIQUE (workspace_id, sha256)` violation from a concurrent race is also 409 (no DB details exposed). The same PDF in another workspace is allowed.
- **Storage service** (`StorageService`):
  - server-generated keys `papers/YYYY/MM/<uuid4>.pdf`, checked against a strict allow-list pattern, then `resolve()` + `is_relative_to(root)` (blocks `..`, absolute paths, drive letters, UNC paths, backslashes and symlink escapes);
  - staging in `<root>/.tmp/`, then an atomic `os.replace` into place (same volume).
- **Cleanup guarantees:**
  - temp file removed on any failure, including cancellation;
  - permanent file removed if INSERT/COMMIT fails (duplicate race, FK violation when the workspace vanishes mid-upload → 404, any other error → generic 500).
- **Delete integration:**
  - `DELETE … RETURNING storage_key` (owner-scoped), then COMMIT, then delete the file;
  - if the file deletion fails: 204 anyway, the failure is logged, the key is appended to `<root>/.cleanup/pending-deletions.jsonl`, and the row is never recreated;
  - `StorageService.retry_pending_deletions()` replays the ledger.
- Errors: `PayloadTooLargeError` (413) and `UnsupportedMediaTypeError` (415) added; existing envelope reused.
- `.gitignore`: `*.pdf` and `*.part` added (`storage/` was already ignored).

### 2. Files
**New:**
- backend/app/services/storage_service.py
- backend/app/services/pdf_validation.py
- backend/app/api/uploads.py (size-limited single-file multipart parsing + OpenAPI request body)
- backend/tests/unit/__init__.py
- backend/tests/unit/test_storage_service.py
- backend/tests/unit/test_pdf_validation.py
- backend/tests/integration/test_paper_upload_api.py

**Modified:**
- backend/app/core/config.py (`paper_storage_dir`, `max_upload_size_mb`, `paper_storage_root`, `max_upload_bytes`)
- backend/app/core/exceptions.py (413, 415)
- backend/app/db/errors.py (`is_foreign_key_violation`)
- backend/app/api/deps.py (`get_storage`, `StorageDep`)
- backend/app/api/v1/endpoints/papers.py (upload endpoint; `StorageDep` passed to the service)
- backend/app/repositories/paper_repository.py (`create`, `sha256_exists_in_owned_workspace`; `delete_owned` now returns `storage_key | None` instead of `bool`)
- backend/app/services/paper_service.py (`upload_pdf`, `require_owned_workspace`, delete removes the file)
- backend/tests/conftest.py (`unit_env` sets a tmp `PAPER_STORAGE_DIR`)
- backend/tests/integration/conftest.py (`storage_root` fixture, `build_api_client` with `raise_server_exceptions` and extra env)
- backend/tests/integration/test_papers_api.py (one assertion: `delete_owned(...) is None`, following the return-type change)
- .env.example, .gitignore, README.md, PROGRESS.md

### 3. Architecture
```
endpoint (auth → ownership → size-limited multipart parse)
  → PaperService.upload_pdf (validate name/MIME → stage → pre-check → commit file → INSERT/COMMIT, cleanup on failure)
      → pdf_validation (pure functions)   → StorageService (keys, paths, temp, atomic move, delete, ledger)
      → PaperRepository (SQL, owner filter)
```
Blocking file writes run in the thread pool (`run_in_threadpool`). Cleanup paths are synchronous so they also run while a request is being cancelled.

### 4. Database schema changes
**None. No migration.** Every column and constraint used (`storage_key UNIQUE`, `UNIQUE (workspace_id, sha256)`, FK to workspaces, CHECKs) already existed in `0001`. `alembic check` reports "No new upgrade operations detected."

### 5. Dependencies introduced
None. Uses only the standard library, FastAPI/Starlette and python-multipart (already installed since Day 2).

### 6. Known limitations
- **Two copies on disk while uploading:** Starlette spools the uploaded part to an OS temp file (in memory up to 1 MiB) while parsing, and SYNTHORA then copies it into `<root>/.tmp/`. Disk use per upload is bounded by the limit + 64 KiB at each step. A fully streaming parser would remove one copy; deferred.
- **Filename/MIME are checked after the body is received:** the part headers are only available once Starlette has parsed the body. The body itself is capped, and auth/ownership already run before any of it is read.
- **Filesystem and DB are not one transaction.**
  - On upload, the file is committed before the row; a process crash between the two leaves an orphan file with no row.
  - On delete, the row is committed before the file; a crash after the commit leaves an orphan file that isn't in the ledger.
  - Both are detectable by a future reconciliation job (files under `papers/` with no matching `storage_key`). That job isn't built yet.
- **Ledger retry is manual:** the README one-liner. There's no scheduler until Celery (Day 10).
- **Rows created before Day 4** (Day 3 manual `manual/…` keys) have no file. Deleting them works and logs "invalid storage key".
- Only a header check: structurally broken PDFs, encrypted PDFs and PDFs with active content are accepted. Day 5 extraction must handle them and set `status = failed` with a safe message.
- No per-user upload quota or rate limiting.
- Carried over: no token revocation or login rate limiting.

### 6a. Requirements carried forward
- **Day 5 processing errors** (unchanged from Day 3): log the full exception server-side, store only a short internal summary in `error_message`, never add it back to `PaperRead`, and use a separate fixed-code field if users need a reason.
- **Day 5 extraction** reads the file via `StorageService.resolve(storage_key)` only, never via a client-supplied path.
- **Day 10:** schedule `retry_pending_deletions()` and an orphan-file reconciliation task.

### 7. Test results (Claude's sandbox, PostgreSQL 16.15; NOT yet your machine)
- `python -m compileall app`: OK.
- `python -m pytest` (no DB): **161 passed, 208 skipped**.
- `RUN_DB_TESTS=1 python -m pytest`: **369 passed, 0 failed**. That's the Day 3 baseline of 218, plus 95 new unit tests and 56 new upload integration tests.
- `alembic check`: "No new upgrade operations detected." `alembic/versions/` still contains only `0001_initial_schema.py`.
- The real `storage/` directory didn't exist before or after the full run (tests use tmp storage only). The stand-in dev DB sentinel was unchanged.
- **Concurrency is genuinely exercised:** a temporary probe (deleted afterwards) ran 10 rounds of 6 parallel identical uploads. Every round gave exactly 1×201 + 5×409, one row and one file. **19 of the 50 losers passed the application pre-check and were stopped by the PostgreSQL unique constraint.**
- **Mutation checks** (each change was reverted afterwards):

  | Safety mechanism removed | Tests that failed |
  |---|---|
  | Receive-level byte counter | the two "small file, huge body" tests |
  | Cleanup after a failed INSERT | 5 tests (insert failure, FK race, unique race, concurrency) |
  | File removal on delete | 2 delete tests |
- **Bugs found and fixed during testing:** Starlette converts `MultiPartException` into `HTTPException(400)` carrying parser details (e.g. "Maximum number of fields is 0"). That is now translated into our own `BAD_REQUEST` message.

### 8. Remaining tasks
- Local verification of Day 4 (blocks completion), then commit after review.
- Day 5: PyMuPDF text and page extraction + GROBID header/section extraction into `paper_sections`; `status` transitions `uploaded → processing → processed | failed`; `page_count`; safe failure codes.
- Day 6: chunking into `paper_chunks` (page-anchored).
- Day 7: migration 0002 (embeddings) + hybrid retrieval (pgvector + `rank_bm25`).

### 9. Exact next development milestone
On Windows:
- `python -m compileall app`;
- `python -m pytest` (expect 161 passed, 208 skipped);
- `$env:RUN_DB_TESTS="1"; python -m pytest` (expect 369 passed);
- `alembic check`;
- the README "Manual testing" walkthrough with a real PDF;
- confirm `git status` shows no `.env`, PDFs or `storage/`.

Then review and commit. Then Day 5: extraction.

---

## Session: Day 3: Paper management API (2026-11-01)

**Status: verified locally by the user (218 tests passed) and committed (`c38cd01`).**

### 1. Completed tasks
- `GET /api/v1/workspaces/{workspace_id}/papers`:
  - paginated (limit 1–100, default 20; offset ≥ 0, default 0);
  - ordered `created_at DESC, id DESC` (deterministic);
  - returns the total;
  - another user's workspace, or a missing one → 404.
- `GET /api/v1/papers/{paper_id}`: owner-only. Not found and owned by another user return the same 404.
- `PATCH /api/v1/papers/{paper_id}`:
  - editable fields: `title`, `abstract`, `authors`, `publication_year`, `doi`; partial updates, and `null` clears a field;
  - rejected: empty body, unknown fields and every internal field (`id`, `workspace_id`, `storage_key`, `sha256`, `file_size_bytes`, `page_count`, `status`, `error_message`, `processed_at`, `original_filename`, `owner_id`, `created_at`);
  - DOIs validated (`10.NNNN/…`) and URL/`doi:` prefixes stripped;
  - publication year limited to 1800–2100 (same range as the DB CHECK).
- `DELETE /api/v1/papers/{paper_id}`: 204. The DB row is deleted, and sections/chunks go with it via the existing cascade.
- Ownership enforced inside each SQL statement (`papers.workspace_id IN (SELECT id FROM workspaces WHERE owner_id = :uid)`):
  - GET is a SELECT; PATCH is a single `UPDATE … RETURNING`; DELETE is a single `DELETE … RETURNING`;
  - no fetch-then-check.
- `PaperRead` never exposes `storage_key` **or `error_message`**. A failed paper shows `"status": "failed"`, and the raw error stays in the DB column for server-side use (final review, 2026-11-01).
- Removed the stale Day 1 test `backend/tests/test_db_integration.py` (`git rm`). It bypassed the test-DB guard and ran against the **development** DB (read-only health check), and it fully duplicated `tests/integration/test_health_db.py`. Now no test can reach the dev DB.
- Fixed JSONB null handling: `Paper.authors` now uses `JSONB(none_as_null=True)`, so clearing authors stores SQL NULL instead of the JSON value `'null'`. Python-side only: same DDL, `alembic check` clean, **no migration**.

### 2. Modified files
**New:**
- backend/app/schemas/paper.py
- backend/app/repositories/paper_repository.py
- backend/app/services/paper_service.py
- backend/app/api/v1/endpoints/papers.py
- backend/tests/integration/test_papers_api.py

**Modified:**
- backend/app/api/v1/router.py (include papers router)
- backend/app/models/paper.py (`authors` JSONB `none_as_null=True`; no schema/DDL change)
- README.md (paper endpoints, full Swagger manual-testing guide, re-runnable demo seeding + cleanup SQL)
- PROGRESS.md

**Deleted (staged with `git rm`):**
- backend/tests/test_db_integration.py

**Final-review additions in backend/tests/integration/test_papers_api.py:**
- `test_internal_error_details_and_paths_never_exposed`: seeds a `failed` paper whose `error_message` and `storage_key` contain a traceback, internal paths and an internal hostname. It asserts none of them appear in the GET single-paper, list or PATCH responses, that `status: "failed"` is still visible, and that the DB column is unchanged.
- `test_openapi_paper_schema_omits_internal_fields`: the published `PaperRead` schema contains neither `storage_key` nor `error_message`.

### 3. Current architecture
Unchanged layering: endpoints → services → repositories.
- `PaperService` uses `WorkspaceRepository.get_owned` (so another user's workspace returns 404) plus `PaperRepository`, where every query carries the owner filter.
- The ownership check is layered: the service checks the workspace and the repository filters every query. Tests cover both layers independently.

### 4. Database schema changes
None. No new migration; `alembic check` → "No new upgrade operations detected."

### 5. Dependencies introduced
None.

### 6. Known bugs / limitations
- **The stored PDF file is not deleted** by `DELETE /papers/{id}`. Only the DB record is removed. No storage service or file path exists until Day 4/5, so no filesystem path is touched today. The Day 4/5 storage service must delete the file *after* the DB commit (or record orphaned keys for cleanup).
- There's no paper create endpoint until Day 4 (upload). Tests seed rows directly into the guarded test DB; manual testing seeds demo rows (README).
- Users see only `status: "failed"`, with no reason, until a safe failure-reason field exists (see the Day 5 requirements below).
- Total count uses a separate `COUNT(*)` query (fine at MVP scale).
- Carried over from Day 2: no token revocation and no login rate limiting.

### 6a. Requirements carried forward (from the Day 3 final review)
**Day 4/5 storage service:**
- `DELETE /papers/{id}` must also remove the stored file: *after* the DB commit, using only the server-generated `storage_key` (never client input). Failures are logged, not hidden.

**Day 5 processing errors:**
- Log the full exception server-side: `logger.exception("Processing failed for paper %s", paper_id)`.
- Store only a short internal summary in `papers.error_message` (no secrets).
- Never add `error_message` back to `PaperRead`.
- If users need a reason, add a separate field restricted to a fixed set of codes (e.g. `invalid_pdf`, `encrypted_pdf`, `extraction_timeout`, `grobid_unavailable`). That would need a migration, decided on Day 5.
- `test_internal_error_details_and_paths_never_exposed` must keep passing.

### 7. Test results (Claude's sandbox, PostgreSQL 16.15; NOT yet your machine)
After the final-review changes:
- `python -m compileall app`: OK.
- `pytest` (no DB): **66 passed, 152 skipped**.
- `RUN_DB_TESTS=1 pytest`: **218 passed, 0 failed**.
  - That's the Day 2 baseline of 140, minus 1 deleted stale test, plus 79 paper tests.
  - The 79 paper tests are 77 from the initial implementation and 2 from the final review.
- `tests/integration/test_papers_api.py` alone: **79 passed**.
- `alembic check`: "No new upgrade operations detected." (no migration).
- Mutation checks (each change was reverted afterwards):
  - removing the repository owner filter makes the repository-level test fail;
  - removing `none_as_null` makes the clear-authors test fail;
  - re-adding `error_message` to `PaperRead` makes both new leak tests fail.
- README seed and cleanup SQL executed for real (twice) on the sandbox test DB: re-runnable, and the cleanup removes only `manual/%` rows.
- Stand-in dev DB sentinel unchanged after the full run.

### 8. Remaining tasks
- Local verification of Day 3 (blocks completion).
- Day 4: PDF upload (`POST /workspaces/{id}/papers`) with:
  - signature (`%PDF-`), MIME and size validation;
  - streaming to an isolated temp file;
  - SHA-256 → duplicate detection (409 via `uq_papers_workspace_id_sha256`);
  - server-generated `storage_key` and a storage service (store, open, delete) with path-traversal protection;
  - wiring file deletion into `DELETE /papers/{id}` (see 6a).
- Days 5–6: PyMuPDF + GROBID extraction into sections and chunks; status transitions.
- Day 7: migration 0002 (embeddings) + hybrid retrieval (pgvector + `rank_bm25`).

### 9. Exact next development milestone
Run the Day 3 local verification on Windows:
- `python -m compileall app`;
- `pytest` (expect 66 passed, 152 skipped);
- `RUN_DB_TESTS=1 pytest` (expect 218 passed);
- `alembic check`;
- the README Swagger walkthrough.

Then commit (Day 3 is marked complete only after this). Then Day 4: storage service + PDF upload endpoint.

---

## Session: Day 2: Database architecture, migrations, JWT auth (2026-10-31)

**Status: verified locally by the user and committed (`77fff18`, Day 2 baseline 140 tests passed incl. the stale Day 1 file).**

### 1. Completed tasks
- Branding renamed to SYNTHORA (APP_NAME, README, package name). Compose project, container, DB name, DB user and volume deliberately unchanged.
- SQLAlchemy declarative `Base` with a constraint naming convention; UUID PK mixin (Python `uuid4` plus server `gen_random_uuid()`) and timestamp mixin; `eager_defaults`.
- Five models: User, Workspace, Paper, PaperSection, PaperChunk. All relationships use `lazy="raise"`; deletes cascade in the DB (`passive_deletes`).
- **Paper ↔ section integrity:** composite FK `paper_chunks(section_id, paper_id) → paper_sections(id, paper_id) ON DELETE SET NULL (section_id)` (PG15+ column list).
- **Paper ↔ workspace integrity:** composite FK `paper_chunks(paper_id, workspace_id) → papers(id, workspace_id) ON DELETE CASCADE`.
- Async Alembic (`alembic/env.py` builds its URL from Settings; tests inject the test-DB URL). Migration `0001_initial_schema` was autogenerated, then hand-reviewed.
- JWT auth (PyJWT):
  - HS256/384/512 only, with an explicit single-algorithm allow-list;
  - required claims `sub, exp, iat, nbf, iss, aud, type`;
  - `sub` must be a UUID and `type` must be `access`;
  - expiry configurable within 5–1440 minutes (default 30);
  - minimum secret length depends on the algorithm (32/48/64 bytes);
  - the placeholder secret is rejected.
- Passwords hashed with argon2id (argon2-cffi), with rehash-on-login when parameters change and a dummy verify for unknown emails (prevents timing-based email enumeration).
- `get_current_user` re-loads the user on every request, so deleted or inactive users are rejected immediately.
- Repository layer (flush only) + service layer (owns the commit, maps unique violations to 409 by constraint name).
- Ownership enforced inside SQL: `WHERE id = :id AND owner_id = :uid`, and `UPDATE … RETURNING` / `DELETE … RETURNING` as single statements. Another user's workspace returns 404.
- Input schemas use `extra="forbid"` (blocks mass assignment of `owner_id`, `id`, `is_active`, `email`).
- Endpoints: auth register/login, users/me (GET/PATCH/DELETE), workspaces CRUD + pagination.
- **Security fixes found by tests during Day 2:**
  - Pydantic validation errors echoed raw setting values (JWT secret, DB password). Fixed with `hide_input_in_errors=True`.
  - The HTTP exception handler dropped `WWW-Authenticate` headers. Now passed through.
- Integration tests isolated on a guarded `researchmind_test` DB (name must end in `_test`, differ from `POSTGRES_DB` and be a plain identifier; the DB is created if missing and never dropped; data reset with TRUNCATE).

### 2. Modified files
**Modified (complete files):**
- .env.example, README.md, PROGRESS.md
- backend/{requirements.txt, requirements-dev.txt, pyproject.toml}
- backend/app/__init__.py (0.2.0)
- backend/app/core/{config.py, exceptions.py}
- backend/app/api/v1/router.py
- backend/tests/{conftest.py, test_config.py}

**Moved:** backend/tests/test_db_integration.py → backend/tests/integration/test_health_db.py (now runs on the test DB)

**New:**
- backend/alembic.ini, backend/alembic/{env.py, script.py.mako, versions/0001_initial_schema.py}
- backend/app/db/{base.py, errors.py}
- backend/app/core/security.py
- backend/app/models/{__init__.py, user.py, workspace.py, paper.py, paper_section.py, paper_chunk.py}
- backend/app/schemas/{common.py, user.py, auth.py, workspace.py}
- backend/app/repositories/{__init__.py, user_repository.py, workspace_repository.py}
- backend/app/services/{auth_service.py, user_service.py, workspace_service.py}
- backend/app/api/deps.py, backend/app/api/v1/endpoints/{auth.py, users.py, workspaces.py}
- backend/tests/{db_safety.py, test_db_safety.py, test_security.py}
- backend/tests/integration/{__init__.py, conftest.py, test_health_db.py, test_migrations.py, test_schema_constraints.py, test_auth_api.py, test_users_api.py, test_workspaces_api.py}

**Unchanged from Day 1:** docker-compose.yml, infra/postgres/init/01-extensions.sql, app/main.py, app/core/logging.py, app/db/session.py, app/services/health_service.py, app/api/v1/endpoints/health.py, app/schemas/{error,health}.py, tests/test_health.py

### 3. Current architecture
```
endpoints (HTTP only) → services (rules, transaction/commit, error mapping) → repositories (SQL, owner filters, flush)
         ↑ api/deps.py: DbSession, SettingsDep, CurrentUser (JWT → user reloaded from DB)
core/: config, security (argon2id + JWT), exceptions, logging     db/: base, session, errors     models/  schemas/
```

### 4. Database schema changes (migration 0001)
- `CREATE EXTENSION IF NOT EXISTS vector` (no-op on the Day 1 volume; never dropped by downgrade).
- **users**: email UNIQUE + CHECK lowercase, hashed_password, full_name, is_active.
- **workspaces**: owner_id → users CASCADE; CHECK non-blank name; UNIQUE INDEX (owner_id, lower(name)).
- **papers**:
  - columns: workspace_id → workspaces CASCADE; original_filename, storage_key UNIQUE, sha256 (CHECK hex), size (CHECK > 0), page_count, status (CHECK in 4 values), GROBID metadata columns (nullable);
  - unique: UNIQUE (workspace_id, sha256), UNIQUE (id, workspace_id);
  - indexes: (workspace_id, status), (workspace_id, created_at).
- **paper_sections**:
  - columns: paper_id → papers CASCADE; order_index, level, heading, section_type (CHECK), text, page range (CHECK);
  - unique: UNIQUE (paper_id, order_index), UNIQUE (id, paper_id).
- **paper_chunks**:
  - foreign keys: the two composite FKs above;
  - checks: chunk_index ≥ 0, token_count > 0, page range;
  - unique: UNIQUE (paper_id, chunk_index);
  - indexes: workspace_id, section_id;
  - **no embedding columns yet.**

### 5. Dependencies introduced
- Runtime: alembic 1.20.1, argon2-cffi 25.1.0, PyJWT 2.15.1, email-validator 2.3.0, python-multipart 0.0.32.
- Dev: pytest-asyncio 1.4.1.
- Transitive: argon2-cffi-bindings 26.1.0, Mako 1.4.3, dnspython 2.9.1.

### 6. Known bugs / limitations
- No token revocation: an access token stays valid until it expires, even after a password change (acceptable without refresh tokens; expiry defaults to 30 min).
- No login rate limiting / lockout yet.
- `updated_at` is maintained by SQLAlchemy, not by a DB trigger, so raw SQL updates don't change it.
- Deleting a user or workspace cascades DB rows only. Uploaded files on disk (Day 4) will need explicit cleanup.
- `/health/db` still exposes server/pgvector versions (restrict before public deployment).
- The registration 409 reveals that an email is registered (common trade-off; revisit if needed).
- `alembic downgrade base` drops all app data. Only acceptable on the dev DB while it is empty.

### 7. Test results (Claude's sandbox; NOT yet your machine)
- `pytest` without a DB: **66 passed, 73 skipped** (integration tests auto-skipped).
- `RUN_DB_TESTS=1 pytest` on PostgreSQL 16.15: **139 passed, 0 failed**.
- Stand-in dev DB with a sentinel row: unchanged after the full test run.
- Guard check: `TEST_POSTGRES_DB=researchmind` aborts with `UnsafeTestDatabaseError` before connecting.
- Dev-path migration on a DB with `vector` pre-installed (Day 1 state): upgrade → check → downgrade → upgrade all OK; extension kept.
- Live uvicorn smoke test: register, login, create, list, and 401 without a token all OK. A log scan at DEBUG found 0 occurrences of the JWT secret, token, user password or DB password.

### 8. Remaining tasks
- **Local verification on Windows + Docker** (blocks Day 2 completion).
- Day 3:
  - paper read APIs scoped through workspace ownership (`papers JOIN workspaces WHERE owner_id`);
  - section/chunk repositories;
  - an ownership test matrix for papers;
  - pin the Docker image digest.
- Days 4–6: upload pipeline (validation, server-side storage keys, sha256 dedupe), PyMuPDF + GROBID ingestion into sections/chunks.
- Day 7: migration 0002 adds `embedding` + `embedding_model` after the embedding model is evaluated; hybrid retrieval = pgvector + Python `rank_bm25` (not `ts_rank`).

### 9. Exact next development milestone
Run the local verification commands in the README / Day 2 report. Report `alembic current` = `0001 (head)`, the `\d paper_chunks` output and `RUN_DB_TESTS=1 pytest` = all passed. Then Day 3 starts: the paper repository and read APIs with ownership enforced through the workspace join.

---

## Session: Day 1: Backend foundation (2026-10-30)

**Status: verified locally by the user on Windows + Docker Compose (2026-10-30).**

### 1. Completed tasks
- Modular FastAPI backend skeleton (app factory + lifespan).
- Typed configuration via pydantic-settings, single root `.env`, secrets as `SecretStr`.
- Async SQLAlchemy 2.0 engine (asyncpg) with pooling, pre-ping, connect timeout; per-request session dependency with rollback on error.
- Docker Compose: PostgreSQL 16 + pgvector, bound to 127.0.0.1:5433, healthcheck, named volume.
- pgvector enabled via init SQL script.
- `GET /api/v1/health` (liveness) and `GET /api/v1/health/db` (readiness).
- Consistent error envelope `{"error": {code, message, details}}`.
- Tests: 14/14 passing.

### 5. Dependencies introduced
fastapi 0.162.0, uvicorn[standard] 0.54.0, SQLAlchemy[asyncio] 2.0.55, asyncpg 0.32.0,
pydantic 2.14.2, pydantic-settings 2.16.2; dev: pytest 9.2.0, httpx2 2.16.0.
