"""Day 6: claim fencing between overlapping extraction workers (stale recovery).

Two real ExtractionService instances (worker A and worker B), each with its own
database session, run concurrently in one event loop. The PDF engine is wrapped by a
gate, so the test decides exactly when each worker's engine call may finish:

    A claims ─▶ A's engine waits ─▶ A's claim is made stale ─▶ B reclaims ─▶ ...

Whatever order the remaining steps take, the superseded worker A must not change the
paper or any of its extracted rows; the newest claim alone decides the outcome, and
every keep-last-good guarantee still holds. Only the guarded test database is used.
"""

import asyncio
import threading
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.core.exceptions import ConflictError
from app.services import extraction_service
from app.services.extraction_service import ExtractionService
from app.services.pdf_extraction import ExtractionFailure
from app.services.storage_service import StorageService
from tests.integration.conftest import register_and_login
from tests.pdf_factory import build_pdf, paragraph

PDF = build_pdf([paragraph(f"Page{i}", 120) for i in range(1, 4)])
WAIT_SECONDS = 20


class GatedEngine:
    """Replaces extract_pdf. Call n (0 = first worker to reach the engine) blocks until
    release(n); it can be told to fail with a handled or an unexpected error."""

    def __init__(self, real: Callable[..., Any]) -> None:
        self._real = real
        self._lock = threading.Lock()
        self._calls = 0
        self._entered = [threading.Event() for _ in range(4)]
        self._released = [threading.Event() for _ in range(4)]
        self.failures: dict[int, Exception] = {}

    def __call__(self, path: Any, **kwargs: Any) -> Any:
        with self._lock:
            index = self._calls
            self._calls += 1
        self._entered[index].set()
        if not self._released[index].wait(WAIT_SECONDS):
            raise TimeoutError(f"engine call {index} was never released")
        if index in self.failures:
            raise self.failures[index]
        return self._real(path, **kwargs)

    async def entered(self, index: int) -> None:
        assert await asyncio.to_thread(self._entered[index].wait, WAIT_SECONDS), f"call {index} never started"

    def release(self, index: int) -> None:
        self._released[index].set()


@pytest.fixture
def owner(api_client: TestClient) -> tuple[dict[str, str], uuid.UUID]:
    headers = register_and_login(api_client, "workers@example.com")
    user_id = uuid.UUID(api_client.get("/api/v1/users/me", headers=headers).json()["id"])
    return headers, user_id


@pytest.fixture
def engine_gate(monkeypatch: pytest.MonkeyPatch) -> GatedEngine:
    gate = GatedEngine(extraction_service.extract_pdf)
    monkeypatch.setattr(extraction_service, "extract_pdf", gate)
    return gate


def upload(client: TestClient, headers: dict) -> str:
    ws = client.post("/api/v1/workspaces", json={"name": "W"}, headers=headers).json()["id"]
    response = client.post(
        f"/api/v1/workspaces/{ws}/papers", files={"file": ("p.pdf", PDF, "application/pdf")}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


class Workers:
    """Two independent services/sessions on the test database, plus a third for checks."""

    def __init__(self) -> None:
        settings = get_settings()
        assert settings.postgres_db.endswith("_test")
        self._engine = create_async_engine(settings.database_url, poolclass=NullPool)
        self._factory = async_sessionmaker(self._engine, expire_on_commit=False)
        self._storage = StorageService(settings.paper_storage_root)
        self._settings = settings
        self._sessions: list[AsyncSession] = []

    def service(self) -> ExtractionService:
        session = self._factory()
        self._sessions.append(session)
        return ExtractionService(session, self._storage, self._settings, grobid=None)

    async def sql(self, statement: str, **params: Any) -> list[Any]:
        async with self._engine.begin() as conn:
            assert (await conn.execute(text("SELECT current_database()"))).scalar_one().endswith("_test")
            result = await conn.execute(text(statement), params)
            return list(result.all()) if result.returns_rows else []

    async def paper(self, paper_id: uuid.UUID) -> Any:
        return (await self.sql(
            "SELECT status, failure_code, extraction_claim_id, processed_at, last_extraction_failure_code, "
            "last_extraction_attempt_at, page_count FROM papers WHERE id = :p", p=paper_id,
        ))[0]

    async def snapshot(self, paper_id: uuid.UUID) -> dict[str, list[Any]]:
        return {
            table: await self.sql(f"SELECT * FROM {table} WHERE paper_id = :p ORDER BY id", p=paper_id)
            for table in ("paper_pages", "paper_sections", "paper_chunks", "paper_references")
        }

    async def make_stale(self, paper_id: uuid.UUID) -> None:
        minutes = self._settings.extraction_stale_minutes + 1
        await self.sql(
            f"UPDATE papers SET updated_at = now() - interval '{minutes} minutes' WHERE id = :p", p=paper_id
        )

    async def close(self) -> None:
        for session in self._sessions:
            await session.close()
        await self._engine.dispose()


async def _start_a_then_b(workers: Workers, gate: GatedEngine, user_id: uuid.UUID, paper_id: uuid.UUID):
    """A claims and waits in its engine; A's claim goes stale; B reclaims and waits too."""
    task_a = asyncio.create_task(workers.service().extract(user_id, paper_id))
    await gate.entered(0)
    claim_a = (await workers.paper(paper_id)).extraction_claim_id
    await workers.make_stale(paper_id)
    task_b = asyncio.create_task(workers.service().extract(user_id, paper_id))
    await gate.entered(1)
    claim_b = (await workers.paper(paper_id)).extraction_claim_id
    assert claim_a is not None and claim_b is not None and claim_a != claim_b
    return task_a, task_b, claim_b


def _run(scenario: Callable[[Workers], Any]) -> None:
    async def main() -> None:
        workers = Workers()
        try:
            await scenario(workers)
        finally:
            await workers.close()

    asyncio.run(main())


# === Superseded worker reaches T2 while the takeover is still running ======================


def test_superseded_worker_cannot_persist_while_takeover_runs(
    api_client: TestClient, owner: tuple, engine_gate: GatedEngine
) -> None:
    headers, user_id = owner
    paper_id = uuid.UUID(upload(api_client, headers))
    engine_gate.release(0)  # the first, ordinary extraction (previous good result)
    assert api_client.post(f"/api/v1/papers/{paper_id}/extract", headers=headers).json()["status"] == "processed"
    gate = engine_gate  # call 0 was that first extraction; workers A and B are calls 1 and 2

    async def scenario(workers: Workers) -> None:
        before = await workers.snapshot(paper_id)
        before_row = await workers.paper(paper_id)
        task_a = asyncio.create_task(workers.service().extract(user_id, paper_id))
        await gate.entered(1)
        claim_a = (await workers.paper(paper_id)).extraction_claim_id
        await workers.make_stale(paper_id)
        task_b = asyncio.create_task(workers.service().extract(user_id, paper_id))
        await gate.entered(2)
        claim_b = (await workers.paper(paper_id)).extraction_claim_id
        assert claim_a != claim_b

        gate.release(1)  # A finishes its PDF work first and reaches T2
        with pytest.raises(ConflictError):
            await task_a
        assert await workers.snapshot(paper_id) == before  # A deleted and inserted nothing
        row = await workers.paper(paper_id)
        assert (row.status, row.extraction_claim_id) == ("processing", claim_b)  # B still owns it
        assert (row.processed_at, row.last_extraction_attempt_at) == (
            before_row.processed_at, before_row.last_extraction_attempt_at,
        )

        gate.release(2)
        summary = await task_b
        assert summary.status == "processed"
        after = await workers.snapshot(paper_id)
        assert {r.id for r in after["paper_pages"]}.isdisjoint({r.id for r in before["paper_pages"]})
        row = await workers.paper(paper_id)
        assert (row.status, row.extraction_claim_id, row.last_extraction_failure_code) == ("processed", None, None)
        assert row.processed_at > before_row.processed_at

    _run(scenario)


# === Superseded worker fails: its failure must not be recorded =============================


@pytest.mark.parametrize(
    ("failure", "raised"),
    [(ExtractionFailure("pdf_corrupted", "simulated"), ConflictError), (RuntimeError("bug"), RuntimeError)],
    ids=["handled-failure", "unexpected-error"],
)
def test_superseded_worker_failure_is_not_recorded(
    api_client: TestClient, owner: tuple, engine_gate: GatedEngine, failure: Exception, raised: type
) -> None:
    headers, user_id = owner
    paper_id = uuid.UUID(upload(api_client, headers))
    engine_gate.failures[0] = failure

    async def scenario(workers: Workers) -> None:
        task_a, task_b, claim_b = await _start_a_then_b(workers, engine_gate, user_id, paper_id)
        engine_gate.release(0)
        with pytest.raises(raised):
            await task_a
        row = await workers.paper(paper_id)
        # A's T3 matched no row: no status change, no failure code, no attempt time.
        assert (row.status, row.extraction_claim_id) == ("processing", claim_b)
        assert (row.failure_code, row.last_extraction_failure_code, row.last_extraction_attempt_at) == (None, None, None)

        engine_gate.release(1)
        summary = await task_b
        assert summary.status == "processed"
        row = await workers.paper(paper_id)
        assert (row.status, row.failure_code, row.last_extraction_failure_code, row.extraction_claim_id) == (
            "processed", None, None, None,
        )

    _run(scenario)


# === Takeover finishes first; the old worker arrives later ==================================


def test_old_worker_finishing_after_takeover_completed_is_discarded(
    api_client: TestClient, owner: tuple, engine_gate: GatedEngine
) -> None:
    headers, user_id = owner
    paper_id = uuid.UUID(upload(api_client, headers))

    async def scenario(workers: Workers) -> None:
        task_a, task_b, _ = await _start_a_then_b(workers, engine_gate, user_id, paper_id)
        engine_gate.release(1)
        assert (await task_b).status == "processed"
        after_b = await workers.snapshot(paper_id)
        row_b = await workers.paper(paper_id)

        engine_gate.release(0)
        with pytest.raises(ConflictError):
            await task_a
        assert await workers.snapshot(paper_id) == after_b
        row = await workers.paper(paper_id)
        assert (row.status, row.processed_at, row.extraction_claim_id) == ("processed", row_b.processed_at, None)

    _run(scenario)


# === Keep-last-good still holds when the takeover itself fails ==============================


@pytest.mark.parametrize("has_previous", [True, False], ids=["with-previous-extraction", "first-extraction"])
def test_takeover_failure_follows_keep_last_good(
    api_client: TestClient, owner: tuple, engine_gate: GatedEngine, has_previous: bool
) -> None:
    headers, user_id = owner
    paper_id = uuid.UUID(upload(api_client, headers))
    offset = 0
    if has_previous:
        engine_gate.release(0)
        assert api_client.post(f"/api/v1/papers/{paper_id}/extract", headers=headers).json()["status"] == "processed"
        offset = 1
    a, b = offset, offset + 1
    engine_gate.failures[b] = ExtractionFailure("pdf_corrupted", "simulated")

    async def scenario(workers: Workers) -> None:
        before = await workers.snapshot(paper_id)
        task_a = asyncio.create_task(workers.service().extract(user_id, paper_id))
        await engine_gate.entered(a)
        await workers.make_stale(paper_id)
        task_b = asyncio.create_task(workers.service().extract(user_id, paper_id))
        await engine_gate.entered(b)

        engine_gate.release(a)  # A would have SUCCEEDED, but it no longer owns the claim
        with pytest.raises(ConflictError):
            await task_a
        assert await workers.snapshot(paper_id) == before

        engine_gate.release(b)  # the newest claim fails
        summary = await task_b
        assert summary.status == "failed" and summary.failure_code == "pdf_corrupted"
        assert summary.retained_previous_extraction is has_previous
        assert await workers.snapshot(paper_id) == before  # nothing lost, nothing half-written
        row = await workers.paper(paper_id)
        assert row.extraction_claim_id is None
        assert row.last_extraction_failure_code == "pdf_corrupted"
        if has_previous:
            assert (row.status, row.failure_code) == ("processed", None)
        else:
            assert (row.status, row.failure_code, row.page_count) == ("failed", "pdf_corrupted", None)

    _run(scenario)


# === Claim bookkeeping ======================================================================


def test_claim_is_set_while_processing_and_cleared_afterwards(
    api_client: TestClient, owner: tuple, engine_gate: GatedEngine
) -> None:
    headers, user_id = owner
    paper_id = uuid.UUID(upload(api_client, headers))

    async def scenario(workers: Workers) -> None:
        task = asyncio.create_task(workers.service().extract(user_id, paper_id))
        await engine_gate.entered(0)
        assert (await workers.paper(paper_id)).extraction_claim_id is not None
        engine_gate.release(0)
        await task
        assert (await workers.paper(paper_id)).extraction_claim_id is None
        # Each claim is new: a second extraction gets a different id.
        task = asyncio.create_task(workers.service().extract(user_id, paper_id))
        await engine_gate.entered(1)
        second = (await workers.paper(paper_id)).extraction_claim_id
        engine_gate.release(1)
        await task
        assert second is not None

    _run(scenario)


def test_claim_id_is_never_exposed(api_client: TestClient, owner: tuple, engine_gate: GatedEngine) -> None:
    headers, _ = owner
    paper_id = upload(api_client, headers)
    engine_gate.release(0)
    responses = [
        api_client.post(f"/api/v1/papers/{paper_id}/extract", headers=headers),
        api_client.get(f"/api/v1/papers/{paper_id}", headers=headers),
    ]
    for response in responses:
        assert "claim" not in response.text


def test_claim_requires_processing_status(api_client: TestClient, owner: tuple) -> None:
    headers, _ = owner
    paper_id = upload(api_client, headers)

    async def scenario(workers: Workers) -> None:
        with pytest.raises(Exception) as excinfo:
            await workers.sql(
                "UPDATE papers SET extraction_claim_id = gen_random_uuid() WHERE id = :p", p=uuid.UUID(paper_id)
            )
        assert "ck_papers_claim_only_when_processing" in str(excinfo.value)
        await workers.sql(
            "UPDATE papers SET status = 'processing', extraction_claim_id = gen_random_uuid() WHERE id = :p",
            p=uuid.UUID(paper_id),
        )
        with pytest.raises(Exception) as excinfo:
            await workers.sql("UPDATE papers SET status = 'processed' WHERE id = :p", p=uuid.UUID(paper_id))
        assert "ck_papers_claim_only_when_processing" in str(excinfo.value)

    _run(scenario)


# === Repository layer: every T2/T3 write statement checks the claim itself ==================


async def test_repository_writes_require_the_current_claim(db_session: Any) -> None:
    """Defence in depth: even without the service's locked pre-check, mark_processed and
    record_failed_attempt change nothing for a stale claim id."""
    from datetime import timedelta

    from app.models import Paper, User, Workspace
    from app.repositories.paper_repository import PaperRepository

    user = User(email="claims@example.com", hashed_password="x")
    db_session.add(user)
    await db_session.flush()
    workspace = Workspace(owner_id=user.id, name="W")
    db_session.add(workspace)
    await db_session.flush()
    paper = Paper(workspace_id=workspace.id, original_filename="p.pdf", storage_key="k-claims",
                  sha256="d" * 64, file_size_bytes=1)
    db_session.add(paper)
    await db_session.flush()
    paper_id, owner_id = paper.id, user.id
    await db_session.commit()
    papers = PaperRepository(db_session)

    first = await papers.claim_for_extraction(paper_id, owner_id, stale_after=timedelta(minutes=15))
    await db_session.commit()
    await db_session.execute(
        text("UPDATE papers SET updated_at = now() - interval '1 day' WHERE id = :p"), {"p": paper_id}
    )
    await db_session.commit()
    second = await papers.claim_for_extraction(paper_id, owner_id, stale_after=timedelta(minutes=15))
    await db_session.commit()
    assert first is not None and second is not None and first.claim_id != second.claim_id

    stale = first.claim_id
    assert await papers.mark_processed(
        paper_id, owner_id, claim_id=stale, page_count=1, structure_source="page_fallback", grobid_outcome="disabled"
    ) is None
    assert await papers.record_failed_attempt(
        paper_id, owner_id, claim_id=stale, failure_code="extraction_error", internal_message="x"
    ) is None
    await db_session.commit()
    row = (await db_session.execute(
        text("SELECT status, extraction_claim_id, last_extraction_failure_code FROM papers WHERE id = :p"),
        {"p": paper_id},
    )).one()
    assert (row.status, row.extraction_claim_id, row.last_extraction_failure_code) == (
        "processing", second.claim_id, None,
    )

    recorded = await papers.record_failed_attempt(
        paper_id, owner_id, claim_id=second.claim_id, failure_code="pdf_corrupted", internal_message="x"
    )
    await db_session.commit()
    assert recorded is not None and recorded.status == "failed"
    row = (await db_session.execute(
        text("SELECT status, extraction_claim_id FROM papers WHERE id = :p"), {"p": paper_id}
    )).one()
    assert (row.status, row.extraction_claim_id) == ("failed", None)


# === T2 checks the claim BEFORE deleting or inserting anything ==============================


def test_superseded_worker_never_starts_deleting_or_inserting(
    api_client: TestClient, owner: tuple, engine_gate: GatedEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not only is nothing committed: the superseded worker must not even issue a DELETE or
    INSERT (relying on a later rollback would hold row locks and do wasted writes)."""
    from app.repositories.paper_page_repository import PaperPageRepository
    from app.repositories.paper_repository import PaperRepository
    from app.repositories.paper_structure_repository import PaperStructureRepository

    headers, user_id = owner
    paper_id = uuid.UUID(upload(api_client, headers))
    writes: list[tuple[str, int]] = []
    current_worker: dict[int, int] = {}

    def spy(cls: type, name: str) -> None:
        original = getattr(cls, name)

        async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            writes.append((name, id(self._session)))
            return await original(self, *args, **kwargs)

        monkeypatch.setattr(cls, name, wrapper)

    spy(PaperRepository, "delete_extracted_content")
    spy(PaperRepository, "fill_metadata")
    spy(PaperPageRepository, "insert_pages")
    for name in ("insert_sections", "insert_chunks", "insert_references"):
        spy(PaperStructureRepository, name)

    async def scenario(workers: Workers) -> None:
        service_a = workers.service()
        current_worker[0] = id(service_a._session)
        task_a = asyncio.create_task(service_a.extract(user_id, paper_id))
        await engine_gate.entered(0)
        await workers.make_stale(paper_id)
        task_b = asyncio.create_task(workers.service().extract(user_id, paper_id))
        await engine_gate.entered(1)
        engine_gate.release(0)
        with pytest.raises(ConflictError):
            await task_a
        assert [w for w in writes if w[1] == current_worker[0]] == []  # worker A wrote nothing at all
        engine_gate.release(1)
        assert (await task_b).status == "processed"
        assert {name for name, _ in writes} >= {"delete_extracted_content", "insert_pages", "insert_chunks"}

    _run(scenario)
