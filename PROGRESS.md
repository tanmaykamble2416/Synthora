# SYNTHORA (formerly RESEARCHMIND): PROGRESS LEDGER

Tagline: Intelligence Beyond Papers.

---

## Session: Day 3: Paper management API (2026-11-01)

**Status: implemented and verified in Claude's Linux sandbox (PostgreSQL 16.15).
PENDING: local verification on Windows + Docker Compose. Day 3 is not complete until that passes.**

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
