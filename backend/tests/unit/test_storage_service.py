"""StorageService in isolation: keys, path safety, staging, atomic commit, deletion, cleanup ledger."""

import hashlib
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core.exceptions import BadRequestError
from app.services.pdf_validation import PDF_HEADER_SIZE, require_pdf_signature
from app.services.storage_service import (
    CLEANUP_DIR_NAME,
    PENDING_DELETIONS_FILE,
    TEMP_DIR_NAME,
    EmptyUploadError,
    InvalidStorageKeyError,
    StorageService,
    UploadTooLargeError,
    iter_upload_chunks,
)

PDF = b"%PDF-1.7\n" + os.urandom(3000) + b"\n%%EOF\n"


@pytest.fixture
def storage(tmp_path: Path) -> StorageService:
    return StorageService(tmp_path / "root")


async def _chunks(data: bytes, size: int = 1000) -> AsyncIterator[bytes]:
    for i in range(0, len(data), size):
        yield data[i : i + size]


async def _stage(storage: StorageService, data: bytes, max_bytes: int = 10_000):
    return await storage.stage(
        _chunks(data), max_bytes=max_bytes, validate_header=require_pdf_signature, header_size=PDF_HEADER_SIZE
    )


def _temp_files(storage: StorageService) -> list[Path]:
    temp = storage.root / TEMP_DIR_NAME
    return list(temp.iterdir()) if temp.exists() else []


# --- keys ------------------------------------------------------------------------------


def test_new_key_format_is_server_generated() -> None:
    key = StorageService.new_key(datetime(2026, 11, 3, tzinfo=UTC))
    prefix, year, month, name = key.split("/")
    assert (prefix, year, month) == ("papers", "2026", "11")
    assert name.endswith(".pdf")
    assert uuid.UUID(name.removesuffix(".pdf")).version == 4


def test_new_keys_are_unique() -> None:
    assert len({StorageService.new_key() for _ in range(1000)}) == 1000


def test_resolve_valid_key_stays_inside_root(storage: StorageService) -> None:
    key = storage.new_key()
    path = storage.resolve(key)
    assert path.is_relative_to(storage.root)
    assert path == storage.root / key


@pytest.mark.parametrize(
    "key",
    [
        "../secret.pdf",
        "papers/../../etc/passwd",
        f"papers/2026/11/../../../{uuid.uuid4()}.pdf",
        f"papers/2026/11/{uuid.uuid4()}.pdf/../../x.pdf",
        f"/papers/2026/11/{uuid.uuid4()}.pdf",              # absolute (POSIX)
        f"C:/papers/2026/11/{uuid.uuid4()}.pdf",            # absolute (Windows drive)
        f"C:\\papers\\2026\\11\\{uuid.uuid4()}.pdf",
        f"papers\\2026\\11\\{uuid.uuid4()}.pdf",            # backslashes
        f"\\\\server\\share\\{uuid.uuid4()}.pdf",           # UNC
        f"papers/2026/13/{uuid.uuid4()}.pdf",               # bad month
        f"papers/2026/11/{uuid.uuid4()}.exe",
        f"papers/2026/11/{uuid.uuid4()}.PDF",
        f"papers/2026/11/{str(uuid.uuid4()).upper()}.pdf",
        f"papers/2026/11/{uuid.uuid1()}.pdf",               # not uuid4
        "papers/2026/11/my-paper.pdf",                      # client-chosen name
        f".tmp/{uuid.uuid4().hex}.part",
        f".cleanup/{PENDING_DELETIONS_FILE}",
        f"papers/2026/11/{uuid.uuid4()}.pdf\x00.txt",
        f" papers/2026/11/{uuid.uuid4()}.pdf",
        "",
    ],
)
def test_resolve_rejects_unsafe_keys(storage: StorageService, key: str) -> None:
    with pytest.raises(InvalidStorageKeyError):
        storage.resolve(key)
    assert storage.exists(key) is False


def test_resolve_rejects_non_string(storage: StorageService) -> None:
    with pytest.raises(InvalidStorageKeyError):
        storage.resolve(None)  # type: ignore[arg-type]


def test_symlink_escape_is_blocked(storage: StorageService, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (storage.root / "papers").mkdir(parents=True)
    try:
        (storage.root / "papers" / "2026").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted on this system")
    with pytest.raises(InvalidStorageKeyError):
        storage.resolve(f"papers/2026/11/{uuid.uuid4()}.pdf")


# --- staging -------------------------------------------------------------------------


async def test_stage_hashes_and_sizes_incrementally(storage: StorageService) -> None:
    staged = await _stage(storage, PDF)
    assert staged.size_bytes == len(PDF)
    assert staged.sha256 == hashlib.sha256(PDF).hexdigest()
    assert staged.path.read_bytes() == PDF
    assert staged.path.parent == storage.root / TEMP_DIR_NAME
    assert staged.path.suffix == ".part"


async def test_stage_accepts_exactly_max_bytes(storage: StorageService) -> None:
    data = (PDF * 10)[:5000]
    staged = await _stage(storage, data, max_bytes=5000)
    assert staged.size_bytes == 5000


async def test_stage_rejects_one_byte_over_limit_and_cleans_up(storage: StorageService) -> None:
    with pytest.raises(UploadTooLargeError):
        await _stage(storage, (PDF * 10)[:5001], max_bytes=5000)
    assert _temp_files(storage) == []


async def test_stage_rejects_non_pdf_before_writing(storage: StorageService) -> None:
    with pytest.raises(BadRequestError):
        await _stage(storage, b"MZ\x90\x00" + os.urandom(2000))  # Windows .exe header
    assert _temp_files(storage) == []


@pytest.mark.parametrize("data", [b"%PDF", b"%PDF-1", b"%PDF-x.y and more"])
async def test_stage_rejects_short_or_malformed_headers(storage: StorageService, data: bytes) -> None:
    with pytest.raises(BadRequestError):
        await _stage(storage, data)
    assert _temp_files(storage) == []


async def test_stage_header_split_across_tiny_chunks(storage: StorageService) -> None:
    staged = await storage.stage(
        _chunks(PDF, size=1), max_bytes=10_000, validate_header=require_pdf_signature, header_size=PDF_HEADER_SIZE
    )
    assert staged.sha256 == hashlib.sha256(PDF).hexdigest()


async def test_stage_rejects_empty_upload(storage: StorageService) -> None:
    with pytest.raises(EmptyUploadError):
        await _stage(storage, b"")
    assert _temp_files(storage) == []


async def test_stage_cleans_up_when_source_fails_midway(storage: StorageService) -> None:
    async def broken() -> AsyncIterator[bytes]:
        yield PDF[:500]
        raise ConnectionResetError("client went away")

    with pytest.raises(ConnectionResetError):
        await storage.stage(broken(), max_bytes=10_000, validate_header=require_pdf_signature, header_size=8)
    assert _temp_files(storage) == []


async def test_iter_upload_chunks_reads_until_empty() -> None:
    data = bytearray(b"x" * 2500)

    async def read(size: int) -> bytes:
        chunk = bytes(data[:size])
        del data[:size]
        return chunk

    assert [len(c) async for c in iter_upload_chunks(read, chunk_size=1000)] == [1000, 1000, 500]


# --- commit / discard / delete --------------------------------------------------------


async def test_commit_moves_atomically_and_discard_is_then_noop(storage: StorageService) -> None:
    staged = await _stage(storage, PDF)
    key = storage.new_key()
    final = storage.commit(staged, key)
    assert final.read_bytes() == PDF
    assert not staged.path.exists()
    assert storage.exists(key)
    storage.discard(staged)  # must not fail or touch the committed file
    assert final.exists()
    assert _temp_files(storage) == []


async def test_commit_refuses_to_overwrite(storage: StorageService) -> None:
    key = storage.new_key()
    storage.commit(await _stage(storage, PDF), key)
    second = await _stage(storage, PDF)
    with pytest.raises(FileExistsError):
        storage.commit(second, key)
    storage.discard(second)
    assert _temp_files(storage) == []


async def test_commit_rejects_unsafe_key(storage: StorageService) -> None:
    staged = await _stage(storage, PDF)
    with pytest.raises(InvalidStorageKeyError):
        storage.commit(staged, "../../evil.pdf")
    assert staged.path.exists()  # untouched; caller discards it
    storage.discard(staged)


async def test_delete_existing_missing_and_unsafe(storage: StorageService) -> None:
    key = storage.new_key()
    storage.commit(await _stage(storage, PDF), key)
    assert storage.delete(key) is True
    assert not storage.exists(key)
    assert storage.delete(key) is False  # already gone
    with pytest.raises(InvalidStorageKeyError):
        storage.delete("../outside.pdf")


# --- cleanup ledger ---------------------------------------------------------------------


async def test_pending_deletions_are_recorded_and_retried(storage: StorageService) -> None:
    present = storage.new_key()
    storage.commit(await _stage(storage, PDF), present)
    missing = storage.new_key()

    storage.record_pending_deletion(present, "paper_deleted")
    storage.record_pending_deletion(missing, "paper_deleted")
    storage.record_pending_deletion(present, "paper_deleted")  # duplicate entry
    assert storage.pending_deletions() == [present, missing, present]

    report = storage.retry_pending_deletions()

    assert (report.deleted, report.already_missing, report.still_failing, report.invalid) == (1, 1, 0, 0)
    assert not storage.exists(present)
    assert storage.pending_deletions() == []
    assert not (storage.root / CLEANUP_DIR_NAME / PENDING_DELETIONS_FILE).exists()


async def test_retry_keeps_entries_that_still_fail(storage: StorageService, monkeypatch: pytest.MonkeyPatch) -> None:
    key = storage.new_key()
    storage.commit(await _stage(storage, PDF), key)
    storage.record_pending_deletion(key, "paper_deleted")

    def locked(self: Path, missing_ok: bool = False) -> None:
        raise PermissionError("file is locked")

    monkeypatch.setattr(Path, "unlink", locked)
    report = storage.retry_pending_deletions()
    monkeypatch.undo()

    assert report.still_failing == 1
    assert storage.pending_deletions() == [key]
    assert storage.retry_pending_deletions().deleted == 1
    assert storage.pending_deletions() == []


def test_retry_drops_invalid_and_malformed_entries(storage: StorageService) -> None:
    ledger = storage.root / CLEANUP_DIR_NAME / PENDING_DELETIONS_FILE
    ledger.parent.mkdir(parents=True)
    ledger.write_text('{"storage_key": "../../etc/passwd"}\nnot-json\n', encoding="utf-8")
    report = storage.retry_pending_deletions()
    assert report.invalid == 1
    assert storage.pending_deletions() == []
