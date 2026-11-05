# SYNTHORA

AI-powered research intelligence and literature review platform. *Intelligence Beyond Papers.*

> Status: **Day 6: document structure, GROBID and chunking** (pending local verification).
> Days 1–4 verified; Day 5 committed.
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

# 2. Start PostgreSQL 16 + pgvector (and, optionally, GROBID)
docker compose up -d postgres
docker compose ps                # STATUS must show "healthy"
docker compose up -d grobid      # optional; first pull ~500 MB, ready after ~1 minute

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
| `GROBID_ENABLED` | `false` (`.env.example`: `true`) | Use GROBID for sections, references and metadata. When `false` or unreachable, extraction still succeeds with page-based chunks. |
| `GROBID_URL` | `http://127.0.0.1:8070` | Base URL only (no credentials, path or query). Use `127.0.0.1`, not `localhost`. |
| `GROBID_PORT` | `8070` | Host port that docker-compose publishes GROBID on (keep equal to the port in `GROBID_URL`). |
| `GROBID_TIMEOUT_SECONDS` | `120` | Total time allowed for one full-text request (5–600). |
| `CHUNK_TARGET_WORDS` / `CHUNK_MAX_WORDS` / `CHUNK_MIN_WORDS` | `350` / `500` / `60` | Chunk sizes, in the unit of `CHUNK_TOKEN_COUNTER` (**words** by default). Must satisfy MIN < TARGET ≤ MAX. |
| `CHUNK_TOKEN_COUNTER` | `word_proxy` | `word_proxy` or `tokenizer` (uses `CHUNK_TOKENIZER_FILE`; falls back to `word_proxy` when unavailable). |
| `CHUNK_TOKENIZER_FILE` / `CHUNK_TOKENIZER_NAME` | none / `BAAI/bge-small-en-v1.5` | Local `tokenizer.json` (never downloaded) and the name stored as `tokenizer:<name>`. |
| `EXTRACTION_DETECT_TABLES` | `true` | Detect ruled tables with PyMuPDF so their text becomes separate table chunks. |

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

# Optional: against the real GROBID container (docker compose up -d grobid)
$env:RUN_DB_TESTS="1"; $env:RUN_GROBID_TESTS="1"; python -m pytest tests/integration/test_grobid_live.py -v -s
Remove-Item Env:RUN_DB_TESTS, Env:RUN_GROBID_TESTS
```

Tests never call a real GROBID unless `RUN_GROBID_TESTS=1`: everywhere else `GROBID_ENABLED` is forced to
`false` and GROBID is replaced by an in-process fake that serves the TEI files in `backend/tests/fixtures/tei/`.

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
| GET | `/api/v1/health/grobid` | – | GROBID: `ok`, `disabled` (200) or 503 when enabled but unreachable |
| POST | `/api/v1/auth/register` | – | Create account |
| POST | `/api/v1/auth/login` | – | OAuth2 password form (`username` = email) → bearer token |
| GET / PATCH / DELETE | `/api/v1/users/me` | Bearer | Own account only |
| POST / GET | `/api/v1/workspaces` | Bearer | Create / list own workspaces (`limit`, `offset`) |
| GET / PATCH / DELETE | `/api/v1/workspaces/{id}` | Bearer | Own workspace only (others' → 404) |
| **POST** | `/api/v1/workspaces/{id}/papers` | Bearer | **Upload a PDF** (multipart field `file`) → 201 + `Location` header |
| GET | `/api/v1/workspaces/{id}/papers` | Bearer | List papers in own workspace (`limit` ≤ 100, `offset`; newest first) |
| GET | `/api/v1/papers/{id}` | Bearer | Own paper only (others' → 404) |
| PATCH | `/api/v1/papers/{id}` | Bearer | Edit `title`, `abstract`, `authors`, `publication_year`, `doi`, `venue` only |
| DELETE | `/api/v1/papers/{id}` | Bearer | Delete the paper record, its pages/sections/chunks/references and its stored PDF |
| **POST** | `/api/v1/papers/{id}/extract` | Bearer | **Extract pages, sections, chunks, references and metadata** → 200 `ExtractionSummary` (409 if already running) |
| GET | `/api/v1/papers/{id}/pages` | Bearer | Extracted pages in page order (`limit` ≤ 50, `offset`) |
| GET | `/api/v1/papers/{id}/pages/{page_number}` | Bearer | One extracted page (1-based) |
| GET | `/api/v1/papers/{id}/sections` | Bearer | Sections in reading order (`limit` ≤ 100) |
| GET | `/api/v1/papers/{id}/chunks` | Bearer | Retrieval chunks with section heading and page range (`limit` ≤ 50) |
| GET | `/api/v1/papers/{id}/references` | Bearer | Bibliography parsed by GROBID (`limit` ≤ 100) |

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
**every page** as its own row in `paper_pages`, numbered from 1 exactly as in the PDF. Since Day 6 the same
request also builds sections, retrieval chunks, references and bibliographic metadata (see
[Document structure](#document-structure-grobid-and-chunking-day-6)). Extraction is manual and synchronous
for now (automatic background processing arrives with Celery on Day 10).

### Workflow (Day 6: keep the last good extraction)

```
POST /papers/{id}/extract
  ├─ T1 claim     UPDATE papers SET status='processing', extraction_claim_id=<new random id>
  │               WHERE id, owned, claimable                                                 COMMIT
  │               NOTHING is deleted: the previous extraction stays readable meanwhile.
  ├─ work         PyMuPDF, page by page            (thread pool, process-wide MuPDF lock, NO transaction)
  │               → GROBID over HTTP (optional)    (outside the MuPDF lock, NO transaction)
  │               → TEI parsing + chunking         (thread pool)
  ├─ T2 replace   SELECT … FOR UPDATE; still OUR claim id? → delete old pages/sections/chunks/references,
  │               insert the new ones, fill NULL metadata, status='processed'               COMMIT
  └─ T3 failure   (separate transaction, after rolling back anything that failed; only with OUR claim id)
                  previous extraction exists → status='processed', kept untouched
                  otherwise                  → status='failed', failure_code              COMMIT
                  both: last_extraction_failure_code + last_extraction_attempt_at
```

- **Short transactions.** T1, T2 and T3 take milliseconds. PDF work and the GROBID call happen between them,
  with no database transaction or row lock held.
- **Atomic replacement.** All pages, sections, chunks and references of an extraction are written in one
  transaction (T2) together with the deletion of the previous ones, or not at all. A reader sees either the
  complete previous extraction or the complete new one, never a mix.
- **A failed re-extraction keeps the last good result.** The paper goes back to `processed`; the attempt is
  visible in `last_extraction_failure_code` / `last_extraction_attempt_at` and in the response
  (`status: "failed"`, `retained_previous_extraction: true`, `paper_status: "processed"`).
- **`processed_at`** is the time of the last **successful** extraction.
- **One extraction per paper at a time.** The T1 conditional UPDATE acts as the lock, and a second
  request gets **409**. A claim older than `EXTRACTION_STALE_MINUTES` (judged by `papers.updated_at`, which
  the claim sets to the database's `now()`) can be taken over after a crash.
- **Claim fencing.** Every claim stores a fresh random `extraction_claim_id` (internal, never in a response),
  and the attempt's T2 and T3 write only while that id is still the paper's current claim. If a slow
  attempt is declared stale and another request takes over, the old attempt changes nothing at all: its
  result is discarded with **409** and its failure is not recorded, so the newest claim alone decides the
  outcome. Leaving `processing` always clears the claim (database CHECK `claim_only_when_processing`).

### Processing lifecycle

```
uploaded ──extract──▶ processing ──▶ processed     (pages, chunks and maybe sections saved)
                          │
                          └────────▶ failed        (failure_code set; no pages)  — first extraction only
processed ──extract again──▶ processing ──▶ processed (replaced)  or  processed (kept; last_extraction_failure_code set)
failed    ──extract again──▶ processing …
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

These codes describe a failed **attempt**. On a first extraction the paper becomes `failed` with this
`failure_code`; on a re-extraction the previous result is kept and the code is stored in
`last_extraction_failure_code` instead. GROBID problems are never failures (see below).

| `failure_code` | Cause |
|---|---|
| `file_missing` | The stored file is gone, or the row's storage key is not a server-generated key (e.g. hand-seeded rows) |
| `pdf_corrupted` | PyMuPDF cannot open the file, or it has no pages |
| `pdf_encrypted` | The PDF needs a password to open. Owner-password-only PDFs (permission restrictions) open and are extracted. |
| `too_many_pages` | More than `EXTRACTION_MAX_PAGES` pages |
| `extraction_error` | **Any** page could not be read. The whole extraction fails rather than presenting partial content as complete. |

`no_extractable_text` is a reserved code; it is not used today (scanned PDFs are `processed`).
The technical detail goes to the server log and the internal `papers.error_message` column, never to the
client. Unexpected errors (bugs, including a failed database write in T2) return the generic **500** and are
recorded through T3 as `extraction_error`, so a paper is never left stuck in `processing` and a previous
extraction is never lost.

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

## Document structure (GROBID and chunking, Day 6)

### Two paths, one result shape

| | GROBID used | Page fallback |
|---|---|---|
| When | `GROBID_ENABLED=true` and GROBID returns usable TEI | GROBID disabled, unreachable, busy, slow, or returned unusable TEI |
| `structure_source` / `grobid_outcome` | `grobid` / `used` | `page_fallback` / `disabled`, `unavailable`, `timeout`, `invalid_response` or `error` |
| Sections | Yes: headings, levels (`2.1` → level 2), types, exact page ranges | None |
| Chunks | Per section, never crossing a section | Over the page text in reading order |
| References | Parsed bibliography with raw string, title, authors, year, venue, DOI, pages | None |
| Metadata (title, authors, abstract, year, DOI, venue) | Filled where the paper's field is empty | Never touched |

**GROBID never makes an extraction fail.** PyMuPDF decides success; GROBID only adds structure.
The pair (`structure_source`, `grobid_outcome`) is enforced by a database CHECK constraint, so a paper can
never claim GROBID structure without GROBID having been used.

The GROBID request is fixed by the server: `consolidateHeader=0` and `consolidateCitations=0` (GROBID does
**not** contact CrossRef or any external service, so paper content never leaves your machine),
`includeRawCitations=1`, `segmentSentences=1`, `teiCoordinates=p,head,s,biblStruct,figure,formula`. The PDF is sent as
`paper.pdf`, never under the user's filename. One retry after a 503 (busy); total deadline
`GROBID_TIMEOUT_SECONDS`; responses over 64 MB are rejected; proxy environment variables are ignored.

### Metadata policy: fill only empty fields

Extraction writes a bibliographic field **only while it is empty (SQL NULL)**. Anything already present,
typed by you via `PATCH` or filled by an earlier extraction, is never overwritten; an explicit empty author
list `[]` counts as a value. Every extracted value passes the same validation as a manual edit (length limits,
year 1800–2100, DOI format); invalid values are dropped. The SQL itself uses `COALESCE(column, :value)` as a
second guard. `metadata_fields_filled` in the response lists what was filled. To have a field filled again,
clear it with `PATCH {"field": null}` and re-extract.

### Sections

GROBID's body divisions become `paper_sections` rows in reading order: the abstract first, then numbered
sections and subsections, then back matter such as acknowledgements.

- **Numbers** are kept apart from the heading: `section_number` is `"II."`, `"B."` or `"3.2"` as printed, and
  `heading` is the title only. The level follows the numbering style: Roman → 1, letter → 2, `3.2` → 2.
- **Types** come from conservative heading phrases (`introduction`, `related_work` including "literature
  review/studies", `methodology` including "methods", `experiments`, `results`, `discussion`, `conclusion`,
  `abstract`, `other`). Generic words such as "model", "framework", "overview", "motivation" or "summary" do not
  decide a type. An unclassified subsection inherits its parent's type (`A. Pipeline` under `VII. Methodology`).
  Anything else stays `other`, with its heading kept.
- A heading with no text of its own (a parent directly followed by a subsection) is kept with empty `text`,
  so the outline is complete.
- Formulas and figure captions are kept in their section. Footnotes and the bibliography are not section text.

**Heading repair from PDF evidence** (`structure_repair.py`). GROBID's heading detection is statistical, and on
papers whose headings use the body font it splits, merges or misses them. Before chunking, these cases are
corrected, but **only when the heading is printed as a line of its own in the PDF text** (`II.` followed by the
line `RELATED WORK OR LITERATURE STUDIES`, or one line `VI. Results And Discussion`). Without that evidence,
nothing changes. Text is moved, never invented or dropped.

| GROBID output | Repaired to |
|---|---|
| heading `II.`, title at the start of the paragraph | heading `RELATED WORK OR LITERATURE STUDIES`, number `II.` |
| heading `PROBLEM DOMAIN`, stray `IV.` ending the previous paragraph | number `IV.`; the fragment is removed (it moved) |
| heading `TABLE I. <caption> B. Behavioral Feature Distribution` | heading `Behavioral Feature Distribution` (`B.`); caption kept as a caption block where it is printed |
| heading `DTI = … Income(1)` | a formula in the previous section; its text stays there |
| `… product mapping. VI. Results And Discussion This section …` inside a paragraph | a new section `VI. Results And Discussion` from that point on |

Only Roman and letter numbering is used as evidence; numbered list items (`1. Load the data`) never are.
If the repair itself fails, GROBID's structure is used as it is.

### Chunks

| Rule | Value |
|---|---|
| Target size | 350 words (`CHUNK_TARGET_WORDS`) |
| Hard maximum | 500 words (`CHUNK_MAX_WORDS`), never exceeded |
| Small remainders | a trailing chunk under 60 words (`CHUNK_MIN_WORDS`) is merged into the previous chunk of the same section if it fits |
| Overlap | none |
| Boundaries | whole paragraphs; long paragraphs split at sentence boundaries; a single sentence over the maximum is split at word boundaries |
| Tables | `content_type = "table"` chunks, never packed together with prose; long tables are split at sentence and word boundaries |
| Kept as prose | formulas and figure captions, in reading order (`content_type = "text"`) |
| Never chunked | footnotes, the bibliography (in the fallback: everything after a "References"/"Bibliography" heading in the second half of the document) |
| Pages | exact 1-based `page_start`/`page_end`: GROBID coordinates, else the page whose text contains the paragraph, else the preceding paragraph's page |

**Tables.** Tables GROBID recognises (`<figure type="table">`) keep their caption and cells, one row per line,
cells separated by ` | `. When GROBID returns table cells as ordinary paragraphs, which is common, PyMuPDF's
table finder locates the table on the page. Sentences whose PDF boxes lie at least 60 % inside that region
become the table block, and the remaining sentences stay prose. This works for ruled tables on unrotated
pages. A table without ruling lines is not detected and stays prose; nothing is guessed. Disable it with
`EXTRACTION_DETECT_TABLES=false`. It costs about 0.1 s per page and runs only when GROBID's output is used.

**`token_count` is measured by `token_count_method`.** The Day 6 default is `word_proxy`: whitespace-separated
words, not model tokens. `CHUNK_TOKEN_COUNTER=tokenizer` with `CHUNK_TOKENIZER_FILE=<local tokenizer.json>`
(and the optional `tokenizers` package) counts real tokens instead and labels them `tokenizer:<name>`.
Nothing is ever downloaded: a missing file or package falls back to `word_proxy` with a warning, and the label
says so. Chunks that existed before migration 0003 are labelled `unknown`, and chunks from before migration 0004
have `content_type = null`. Day 7 switches to the embedding model's tokenizer and will not embed `word_proxy`
or `unknown` chunks.

### GROBID service

`docker compose up -d grobid` starts `grobid/grobid:0.9.1-crf` (Apache-2.0; CRF models only, CPU, ~500 MB
image, needs about 4 GB of RAM while processing) on `127.0.0.1:8070` only. It stores nothing, so it has no
volume. Check it with `GET /api/v1/health/grobid` or `docker compose ps` (health check on `/api/isalive`).
Pin the image after the first pull:
`docker image inspect grobid/grobid:0.9.1-crf --format "{{index .RepoDigests 0}}"`.

### Structure limitations

- GROBID's segmentation is statistical: headings, section types, author lists and references can be wrong or
  missing on unusual layouts. Values it cannot determine stay `null`; nothing is invented.
- `section_type` comes from heading phrases; a section titled e.g. "Our Pipeline" is `other`.
- Heading repair needs the heading printed as its own line. Headings GROBID missed that never appear like that
  stay where GROBID put them.
- A table row that GROBID placed in a later section (wrong reading order) is marked as a table there. It is not
  moved back, and a table spanning two pages becomes two table blocks (joining them is deferred, to avoid
  reassigning content wrongly).
- Figure captions are placed by (page, y) position, which can be off within a page of a two-column layout.
- GROBID joins hyphenated compounds at line ends (`data-intensive` → `dataintensive`). This is GROBID's own text.
- When a paragraph has no coordinates and its text cannot be found on any page (e.g. GROBID de-hyphenated a
  word that PyMuPDF kept split), it is given the preceding paragraph's page. The server logs how many.
- The fallback has no section structure and finds the bibliography with a heading heuristic.
- Chunk sizes are in words until Day 7.
- The GROBID call is synchronous and can take tens of seconds for a long paper; Day 10 moves it to Celery.

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
docker compose up -d; docker compose ps          # postgres "healthy"; grobid "healthy" after ~1 min
cd backend; .\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt              # Day 6 adds httpx2 (GROBID client)
alembic upgrade head                             # back up first (PROGRESS.md, Day 6); applies 0003
alembic current                                  # expect: 0003 (head)
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

### 6. Structure (Day 6)
| Step | Request | Expected |
|---|---|---|
| GROBID up | `GET /api/v1/health/grobid` | 200 `{"status": "ok"}` (or `disabled` if `GROBID_ENABLED=false`) |
| Extract | `POST /api/v1/papers/{paper_id}/extract` on a real paper | 200, `structure_source: "grobid"`, `grobid_outcome: "used"`, sensible `section_count`, `chunk_count`, `reference_count` |
| Metadata | `GET /api/v1/papers/{paper_id}` | title, authors, abstract, year, DOI and venue filled **only** where they were empty; compare with the PDF |
| Sections | `GET …/sections` | headings in reading order with page ranges matching the PDF viewer |
| Chunks | `GET …/chunks` | every `token_count` ≤ 500, `token_count_method: "word_proxy"`, page ranges match where the text is in the PDF |
| References | `GET …/references` | one entry per bibliography item, in order |
| Manual edit wins | `PATCH` a different title, extract again | title unchanged, `metadata_fields_filled` without `title` |
| GROBID down | `docker compose stop grobid`, extract again | 200 `processed`, `structure_source: "page_fallback"`, `grobid_outcome: "unavailable"`, sections/references now empty, metadata kept; `docker compose start grobid` afterwards |
| Failed re-run keeps data | temporarily rename the stored PDF under `storage\papers\…`, extract | 200 `status: "failed"`, `retained_previous_extraction: true`; `GET …/chunks` unchanged; rename it back |

### 7. Cross-user isolation
Log out, register `other@example.com` and authorize as that user. Then:
- upload into the demo user's workspace;
- `GET`, `PATCH` and `DELETE` one of the demo user's papers;
- `POST …/extract`, `GET …/pages`, `GET …/pages/1`, `GET …/sections`, `GET …/chunks` and `GET …/references`
  on one of the demo user's papers.

All must return **404**, and the demo user's file must still be on disk.

### 8. Clean up
Delete demo papers with `DELETE /api/v1/papers/{id}`, which removes the files and all extracted content too. Rows seeded by hand
during Day 3 testing (keys starting `manual/`) have no file. Deleting them works and logs
"invalid storage key; nothing was deleted" on the server.

## Notes

- Postgres is published on `127.0.0.1:5433` (not 5432) to avoid clashing with a native Postgres install.
- `infra/postgres/init/*.sql` only runs when the data volume is empty. Alembic is the source of truth for the schema.
- Never run `docker compose down -v`: `-v` deletes the database volume.
- Pin the images after first pull: `docker image inspect pgvector/pgvector:pg16 --format "{{index .RepoDigests 0}}"`
  (same for `grobid/grobid:0.9.1-crf`).
- GROBID is published on `127.0.0.1:8070` only; it has no authentication.
