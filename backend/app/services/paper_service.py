import logging
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import UploadFile

from app.core.exceptions import BadRequestError, ConflictError, NotFoundError, PayloadTooLargeError
from app.db.errors import is_foreign_key_violation, is_unique_violation
from app.models.paper import Paper
from app.repositories.paper_repository import PaperRepository
from app.repositories.workspace_repository import WorkspaceRepository
from app.schemas.common import Page
from app.schemas.paper import PaperRead, PaperUpdate
from app.services.pdf_validation import (
    PDF_HEADER_SIZE,
    check_declared_content_type,
    require_pdf_signature,
    sanitize_filename,
)
from app.services.storage_service import (
    EmptyUploadError,
    InvalidStorageKeyError,
    StorageService,
    UploadTooLargeError,
    iter_upload_chunks,
)

logger = logging.getLogger(__name__)

PAPER_NOT_FOUND = "Paper not found."
WORKSPACE_NOT_FOUND = "Workspace not found."
DUPLICATE_PAPER = "This PDF has already been uploaded to this workspace."
EMPTY_UPLOAD = "The uploaded file is empty."

SHA256_CONSTRAINT = "uq_papers_workspace_id_sha256"
WORKSPACE_FK = "fk_papers_workspace_id_workspaces"


class PaperService:
    """owner_id always comes from the authenticated user. Papers in workspaces the
    user does not own are reported as 'not found' so their existence is not revealed."""

    def __init__(self, session: AsyncSession, storage: StorageService) -> None:
        self._session = session
        self._storage = storage
        self._papers = PaperRepository(session)
        self._workspaces = WorkspaceRepository(session)

    # --- read / update (Day 3) ------------------------------------------------------

    async def require_owned_workspace(self, owner_id: uuid.UUID, workspace_id: uuid.UUID) -> None:
        if await self._workspaces.get_owned(workspace_id, owner_id) is None:
            raise NotFoundError(WORKSPACE_NOT_FOUND)

    async def list_for_workspace(
        self, owner_id: uuid.UUID, workspace_id: uuid.UUID, *, limit: int, offset: int
    ) -> Page[PaperRead]:
        # SQL-scoped lookup so another user's workspace is a 404, not an empty list.
        await self.require_owned_workspace(owner_id, workspace_id)
        items, total = await self._papers.list_in_workspace(
            workspace_id, owner_id, limit=limit, offset=offset
        )
        return Page[PaperRead](
            items=[PaperRead.model_validate(p) for p in items], total=total, limit=limit, offset=offset
        )

    async def get(self, owner_id: uuid.UUID, paper_id: uuid.UUID) -> Paper:
        paper = await self._papers.get_owned(paper_id, owner_id)
        if paper is None:
            raise NotFoundError(PAPER_NOT_FOUND)
        return paper

    async def update_metadata(self, owner_id: uuid.UUID, paper_id: uuid.UUID, data: PaperUpdate) -> Paper:
        paper = await self._papers.update_owned(paper_id, owner_id, data.to_column_values())
        if paper is None:
            raise NotFoundError(PAPER_NOT_FOUND)
        await self._session.commit()
        return paper

    # --- upload (Day 4) ----------------------------------------------------------------

    async def upload_pdf(
        self, owner_id: uuid.UUID, workspace_id: uuid.UUID, upload: UploadFile, *, max_bytes: int
    ) -> Paper:
        """Validate, store and register one PDF. The caller has already checked
        workspace ownership (before reading the request body)."""
        filename = sanitize_filename(upload.filename)
        check_declared_content_type(upload.content_type)

        try:
            staged = await self._storage.stage(
                iter_upload_chunks(upload.read),
                max_bytes=max_bytes,
                validate_header=require_pdf_signature,
                header_size=PDF_HEADER_SIZE,
            )
        except UploadTooLargeError:
            raise PayloadTooLargeError(
                f"The upload exceeds the maximum size of {max_bytes // (1024 * 1024)} MB."
            ) from None
        except EmptyUploadError:
            raise BadRequestError(EMPTY_UPLOAD) from None

        try:
            if await self._papers.sha256_exists_in_owned_workspace(workspace_id, owner_id, staged.sha256):
                raise ConflictError(DUPLICATE_PAPER)
            storage_key = self._storage.new_key()
            self._storage.commit(staged, storage_key)
        finally:
            self._storage.discard(staged)  # no-op once committed

        return await self._register(workspace_id, filename, storage_key, staged.sha256, staged.size_bytes)

    async def _register(
        self, workspace_id: uuid.UUID, filename: str, storage_key: str, sha256: str, size: int
    ) -> Paper:
        """INSERT + COMMIT. If that fails for any reason, the just-stored file is removed."""
        try:
            paper = await self._papers.create(
                workspace_id=workspace_id,
                original_filename=filename,
                storage_key=storage_key,
                sha256=sha256,
                file_size_bytes=size,
            )
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            self._remove_file(storage_key, reason="upload_db_insert_failed")
            if is_unique_violation(exc, SHA256_CONSTRAINT):
                # A concurrent upload of the same PDF won the race past the pre-check.
                raise ConflictError(DUPLICATE_PAPER) from exc
            if is_foreign_key_violation(exc, WORKSPACE_FK):
                raise NotFoundError(WORKSPACE_NOT_FOUND) from exc  # workspace deleted mid-upload
            raise
        except BaseException:
            # Includes cancellation (client disconnect). Cleanup is synchronous on purpose.
            self._remove_file(storage_key, reason="upload_db_insert_failed")
            raise
        logger.info("Stored paper %s (%d bytes) in workspace %s", paper.id, size, workspace_id)
        return paper

    # --- delete ------------------------------------------------------------------------

    async def delete(self, owner_id: uuid.UUID, paper_id: uuid.UUID) -> None:
        """Delete the DB row first (and commit), then the stored file. If the file
        cannot be removed, the row stays deleted and the key is queued for retry."""
        storage_key = await self._papers.delete_owned(paper_id, owner_id)
        if storage_key is None:
            raise NotFoundError(PAPER_NOT_FOUND)
        await self._session.commit()
        self._remove_file(storage_key, reason="paper_deleted", paper_id=paper_id)

    def _remove_file(self, storage_key: str, *, reason: str, paper_id: uuid.UUID | None = None) -> None:
        """Best-effort removal of a stored file. Never raises; never exposes paths."""
        label = f"paper {paper_id}" if paper_id else "an aborted upload"
        try:
            if not self._storage.delete(storage_key):
                logger.warning("Stored file for %s was already missing", label)
        except InvalidStorageKeyError:
            # Not a server-generated key (e.g. rows created before Day 4): nothing safe to delete.
            logger.error("Stored file for %s has an invalid storage key; nothing was deleted", label)
        except OSError:
            logger.exception("Could not delete stored file for %s; queued for retry", label)
            try:
                self._storage.record_pending_deletion(storage_key, reason)
            except OSError:
                logger.exception("Could not record pending deletion for %s; manual cleanup needed", label)
