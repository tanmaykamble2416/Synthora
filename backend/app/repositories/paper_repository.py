"""Data access for papers.

A paper is owned through its workspace. Every method takes owner_id and applies
`papers.workspace_id IN (SELECT id FROM workspaces WHERE owner_id = :owner_id)`
inside the same SQL statement that reads, updates or deletes the row, so there is
no fetch-then-check step and no method that can touch a paper without an owner filter.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, Select, case, delete, exists, func, literal, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.paper import Paper
from app.models.paper_chunk import PaperChunk
from app.models.paper_page import PaperPage
from app.models.paper_reference import PaperReference
from app.models.paper_section import PaperSection
from app.models.workspace import Workspace

# Statuses from which a new extraction may start ("processing" only once stale).
CLAIMABLE_STATUSES = ("uploaded", "processed", "failed")
# Bibliographic columns that extraction may fill (only while NULL).
METADATA_COLUMNS = ("title", "abstract", "authors", "publication_year", "doi", "venue")


@dataclass(frozen=True, slots=True)
class ClaimedPaper:
    storage_key: str
    workspace_id: uuid.UUID
    # Identifies THIS extraction attempt. Every later write of the attempt (T2, T3)
    # must present it; a stale-recovery takeover replaces it, which fences the old worker out.
    claim_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class LockedPaper:
    status: str
    workspace_id: uuid.UUID
    claim_id: uuid.UUID | None
    metadata: dict[str, Any]

    def is_held_by(self, claim_id: uuid.UUID) -> bool:
        return self.status == "processing" and self.claim_id == claim_id


@dataclass(frozen=True, slots=True)
class RecordedFailure:
    status: str  # "processed" if a previous extraction was retained, else "failed"
    processed_at: datetime | None


def _owned_by(owner_id: uuid.UUID) -> ColumnElement[bool]:
    owned_workspace_ids = select(Workspace.id).where(Workspace.owner_id == owner_id)
    return Paper.workspace_id.in_(owned_workspace_ids)


def owned_paper_ids(owner_id: uuid.UUID) -> Select[tuple[uuid.UUID]]:
    """Sub-select of the ids of every paper the owner may access (for child tables)."""
    return select(Paper.id).where(_owned_by(owner_id))


class PaperRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_owned(self, paper_id: uuid.UUID, owner_id: uuid.UUID) -> Paper | None:
        stmt = select(Paper).where(Paper.id == paper_id, _owned_by(owner_id))
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_in_workspace(
        self, workspace_id: uuid.UUID, owner_id: uuid.UUID, *, limit: int, offset: int
    ) -> tuple[list[Paper], int]:
        criteria = (Paper.workspace_id == workspace_id, _owned_by(owner_id))
        total_stmt = select(func.count()).select_from(Paper).where(*criteria)
        total = (await self._session.execute(total_stmt)).scalar_one()
        stmt = (
            select(Paper)
            .where(*criteria)
            # id breaks ties between papers created in the same instant -> deterministic pages.
            .order_by(Paper.created_at.desc(), Paper.id.desc())
            .limit(limit)
            .offset(offset)
        )
        items = list((await self._session.execute(stmt)).scalars().all())
        return items, total

    async def update_owned(
        self, paper_id: uuid.UUID, owner_id: uuid.UUID, values: dict[str, Any]
    ) -> Paper | None:
        # Single statement: UPDATE papers SET ... WHERE id = :id AND <owned> RETURNING *
        stmt = (
            update(Paper)
            .where(Paper.id == paper_id, _owned_by(owner_id))
            .values(**values)
            .returning(Paper)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def delete_owned(self, paper_id: uuid.UUID, owner_id: uuid.UUID) -> str | None:
        """Delete the paper row; return its storage_key, or None if not found/not owned.
        Sections and chunks are removed by ON DELETE CASCADE."""
        stmt = delete(Paper).where(Paper.id == paper_id, _owned_by(owner_id)).returning(Paper.storage_key)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def sha256_exists_in_owned_workspace(
        self, workspace_id: uuid.UUID, owner_id: uuid.UUID, sha256: str
    ) -> bool:
        """Fast duplicate pre-check. The UNIQUE (workspace_id, sha256) constraint remains
        the real guarantee for concurrent uploads."""
        stmt = select(
            select(Paper.id)
            .where(Paper.workspace_id == workspace_id, Paper.sha256 == sha256, _owned_by(owner_id))
            .exists()
        )
        return bool((await self._session.execute(stmt)).scalar_one())

    async def create(
        self,
        *,
        workspace_id: uuid.UUID,
        original_filename: str,
        storage_key: str,
        sha256: str,
        file_size_bytes: int,
    ) -> Paper:
        # Status defaults to 'uploaded'; page_count and bibliographic metadata stay NULL
        # until extraction (Day 5). Nothing is guessed.
        paper = Paper(
            workspace_id=workspace_id,
            original_filename=original_filename,
            storage_key=storage_key,
            sha256=sha256,
            file_size_bytes=file_size_bytes,
        )
        self._session.add(paper)
        await self._session.flush()
        return paper

    # --- extraction lifecycle (Day 5, revised Day 6) ----------------------------------
    # Every statement still carries the owner filter.
    #
    # Day 6 keeps the last good extraction:
    #   T1 claim_for_extraction      status -> processing, new extraction_claim_id; NOTHING is deleted
    #   T2 lock_for_persist (claim must still match) +
    #      delete_extracted_content + inserts + fill_metadata + mark_processed  (one transaction)
    #   T3 record_failed_attempt     (claim must still match) -> processed (kept) or failed
    # Leaving 'processing' always clears extraction_claim_id (enforced by a CHECK).

    async def claim_for_extraction(
        self, paper_id: uuid.UUID, owner_id: uuid.UUID, *, stale_after: timedelta
    ) -> ClaimedPaper | None:
        """Atomically move the paper to 'processing'. Returns None if it is not
        owned/not found or another extraction is already running (and not stale).

        A single conditional UPDATE acts as the lock: two concurrent requests cannot both
        claim the same paper. updated_at is refreshed by this UPDATE (ORM onupdate), so
        it records when processing started for stale-claim recovery. The cutoff is
        computed by PostgreSQL (now() - interval), so app/DB clock skew cannot matter.
        Existing pages/sections/chunks/references stay readable while processing.

        Each successful claim stores a fresh random extraction_claim_id. A stale takeover
        overwrites it, so the original (slow) worker can no longer match it in T2 or T3."""
        claim_id = uuid.uuid4()
        stmt = (
            update(Paper)
            .where(
                Paper.id == paper_id,
                _owned_by(owner_id),
                or_(
                    Paper.status.in_(CLAIMABLE_STATUSES),
                    (Paper.status == "processing") & (Paper.updated_at < func.now() - stale_after),
                ),
            )
            .values(status="processing", extraction_claim_id=claim_id, failure_code=None, error_message=None)
            .returning(Paper.storage_key, Paper.workspace_id)
        )
        row = (await self._session.execute(stmt)).one_or_none()
        if row is None:
            return None
        return ClaimedPaper(storage_key=row.storage_key, workspace_id=row.workspace_id, claim_id=claim_id)

    async def lock_for_persist(self, paper_id: uuid.UUID, owner_id: uuid.UUID) -> LockedPaper | None:
        """SELECT ... FOR UPDATE: serialises the final write of concurrent extractions and
        any concurrent metadata edit. Returns the current claim (the caller must check
        LockedPaper.is_held_by before writing anything) and the metadata for the NULL check.
        While this lock is held, no other worker can claim, finish or fail the paper."""
        columns = [getattr(Paper, c) for c in METADATA_COLUMNS]
        stmt = (
            select(Paper.status, Paper.workspace_id, Paper.extraction_claim_id, *columns)
            .where(Paper.id == paper_id, _owned_by(owner_id))
            .with_for_update()
        )
        row = (await self._session.execute(stmt)).one_or_none()
        if row is None:
            return None
        return LockedPaper(
            status=row.status,
            workspace_id=row.workspace_id,
            claim_id=row.extraction_claim_id,
            metadata={c: getattr(row, c) for c in METADATA_COLUMNS},
        )

    async def delete_extracted_content(self, paper_id: uuid.UUID, owner_id: uuid.UUID) -> None:
        """Remove pages and everything derived from them. Called ONLY inside T2, in the
        same transaction that inserts the replacement, so a reader sees either the
        complete previous extraction or the complete new one."""
        owned = Paper.id.in_(owned_paper_ids(owner_id))
        target = select(Paper.id).where(Paper.id == paper_id, owned)
        for model in (PaperChunk, PaperReference, PaperSection, PaperPage):  # chunks reference sections
            await self._session.execute(delete(model).where(model.paper_id.in_(target)))

    async def fill_metadata(self, paper_id: uuid.UUID, owner_id: uuid.UUID, values: dict[str, Any]) -> None:
        """Fill-only-NULL: SET col = COALESCE(col, :value). Never overwrites a value."""
        if not values:
            return
        unknown = set(values) - set(METADATA_COLUMNS)
        if unknown:
            raise ValueError(f"not metadata columns: {sorted(unknown)}")
        if any(value is None for value in values.values()):
            raise ValueError("fill_metadata never writes NULL")
        # literal(..., type_=column.type): the bind parameter carries the column's type
        # (JSONB for authors), so COALESCE compares like with like.
        assignments = {
            name: func.coalesce(getattr(Paper, name), literal(value, type_=getattr(Paper, name).type))
            for name, value in values.items()
        }
        stmt = update(Paper).where(Paper.id == paper_id, _owned_by(owner_id)).values(**assignments)
        await self._session.execute(stmt)

    async def mark_processed(
        self,
        paper_id: uuid.UUID,
        owner_id: uuid.UUID,
        *,
        claim_id: uuid.UUID,
        page_count: int,
        structure_source: str,
        grobid_outcome: str,
    ) -> datetime | None:
        stmt = (
            update(Paper)
            .where(
                Paper.id == paper_id,
                _owned_by(owner_id),
                Paper.status == "processing",
                Paper.extraction_claim_id == claim_id,
            )
            .values(
                status="processed",
                extraction_claim_id=None,
                page_count=page_count,
                processed_at=func.now(),
                last_extraction_attempt_at=func.now(),
                last_extraction_failure_code=None,
                failure_code=None,
                error_message=None,
                structure_source=structure_source,
                grobid_outcome=grobid_outcome,
            )
            .returning(Paper.processed_at)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def record_failed_attempt(
        self,
        paper_id: uuid.UUID,
        owner_id: uuid.UUID,
        *,
        claim_id: uuid.UUID,
        failure_code: str,
        internal_message: str,
    ) -> RecordedFailure | None:
        """T3. If the paper still holds a previous successful extraction (it has pages),
        it goes back to 'processed' with that extraction untouched and the failure is
        recorded only in last_extraction_*. Otherwise it becomes 'failed'.
        processed_at (time of the last success) is never changed here.
        Returns None (and changes nothing) unless claim_id is still the current claim."""
        has_previous = exists().where(PaperPage.paper_id == Paper.id)
        stmt = (
            update(Paper)
            .where(
                Paper.id == paper_id,
                _owned_by(owner_id),
                Paper.status == "processing",
                Paper.extraction_claim_id == claim_id,
            )
            .values(
                extraction_claim_id=None,
                status=case((has_previous, "processed"), else_="failed"),
                failure_code=case((has_previous, None), else_=failure_code),
                page_count=case((has_previous, Paper.page_count), else_=None),
                error_message=internal_message,
                last_extraction_failure_code=failure_code,
                last_extraction_attempt_at=func.now(),
            )
            .returning(Paper.status, Paper.processed_at)
        )
        row = (await self._session.execute(stmt)).one_or_none()
        return None if row is None else RecordedFailure(status=row.status, processed_at=row.processed_at)
