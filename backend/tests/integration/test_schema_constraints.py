"""Database-level integrity: composite FKs, CHECKs, UNIQUEs and cascades."""

import hashlib
import uuid

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.errors import violated_constraint
from app.models import Paper, PaperChunk, PaperSection, User, Workspace


def _sha(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


async def _user(session: AsyncSession, email: str = "owner@example.com") -> User:
    user = User(email=email, hashed_password="x")
    session.add(user)
    await session.flush()
    return user


async def _workspace(session: AsyncSession, owner: User, name: str = "WS") -> Workspace:
    ws = Workspace(owner_id=owner.id, name=name)
    session.add(ws)
    await session.flush()
    return ws


async def _paper(session: AsyncSession, ws: Workspace, seed: str = "p1") -> Paper:
    paper = Paper(
        workspace_id=ws.id,
        original_filename=f"{seed}.pdf",
        storage_key=f"papers/{uuid.uuid4()}.pdf",
        sha256=_sha(seed),
        file_size_bytes=1024,
    )
    session.add(paper)
    await session.flush()
    return paper


async def _section(session: AsyncSession, paper: Paper, order: int = 0) -> PaperSection:
    section = PaperSection(paper_id=paper.id, order_index=order, text="Intro text", page_start=1, page_end=2)
    session.add(section)
    await session.flush()
    return section


def _chunk(paper: Paper, *, index: int = 0, section_id: uuid.UUID | None = None, **overrides) -> PaperChunk:
    values = dict(
        paper_id=paper.id,
        workspace_id=paper.workspace_id,
        section_id=section_id,
        chunk_index=index,
        text="chunk text",
        token_count=3,
        page_start=1,
        page_end=1,
    )
    values.update(overrides)
    return PaperChunk(**values)


async def _expect_violation(session: AsyncSession, obj: object, constraint: str) -> None:
    session.add(obj)
    with pytest.raises(IntegrityError) as exc_info:
        await session.flush()
    assert violated_constraint(exc_info.value) == constraint
    await session.rollback()


# --- Paper <-> section integrity (the key Day 2 correction) -----------------------


async def test_chunk_cannot_reference_section_of_another_paper(db_session: AsyncSession) -> None:
    ws = await _workspace(db_session, await _user(db_session))
    paper_a = await _paper(db_session, ws, "a")
    paper_b = await _paper(db_session, ws, "b")
    section_of_b = await _section(db_session, paper_b)

    await _expect_violation(
        db_session,
        _chunk(paper_a, section_id=section_of_b.id),
        "fk_paper_chunks_section_id_paper_id_paper_sections",
    )


async def test_chunk_can_reference_section_of_its_own_paper(db_session: AsyncSession) -> None:
    ws = await _workspace(db_session, await _user(db_session))
    paper = await _paper(db_session, ws)
    section = await _section(db_session, paper)
    chunk = _chunk(paper, section_id=section.id)
    db_session.add(chunk)
    await db_session.commit()
    assert chunk.section_id == section.id


async def test_chunk_without_section_is_allowed(db_session: AsyncSession) -> None:
    ws = await _workspace(db_session, await _user(db_session))
    paper = await _paper(db_session, ws)
    db_session.add(_chunk(paper, section_id=None))
    await db_session.commit()


async def test_deleting_section_nulls_only_section_id(db_session: AsyncSession) -> None:
    ws = await _workspace(db_session, await _user(db_session))
    paper = await _paper(db_session, ws)
    section = await _section(db_session, paper)
    chunk = _chunk(paper, section_id=section.id)
    db_session.add(chunk)
    await db_session.commit()
    chunk_id = chunk.id

    # Raw SQL delete so the database (not the ORM) performs the ON DELETE action.
    await db_session.execute(text("DELETE FROM paper_sections WHERE id = :id"), {"id": section.id})
    await db_session.commit()

    row = (
        await db_session.execute(
            text("SELECT paper_id, workspace_id, section_id FROM paper_chunks WHERE id = :id"),
            {"id": chunk_id},
        )
    ).one()
    assert row.section_id is None
    assert row.paper_id == paper.id
    assert row.workspace_id == ws.id


# --- Paper <-> workspace integrity on chunks ----------------------------------------


async def test_chunk_workspace_must_match_paper_workspace(db_session: AsyncSession) -> None:
    owner = await _user(db_session)
    ws1 = await _workspace(db_session, owner, "one")
    ws2 = await _workspace(db_session, owner, "two")
    paper = await _paper(db_session, ws1)

    await _expect_violation(
        db_session, _chunk(paper, workspace_id=ws2.id), "fk_paper_chunks_paper_id_workspace_id_papers"
    )


async def test_orm_relationship_fills_both_composite_fk_columns(db_session: AsyncSession) -> None:
    ws = await _workspace(db_session, await _user(db_session))
    paper = await _paper(db_session, ws)
    chunk = PaperChunk(paper=paper, chunk_index=0, text="t", token_count=1, page_start=1, page_end=1)
    db_session.add(chunk)
    await db_session.commit()
    assert (chunk.paper_id, chunk.workspace_id) == (paper.id, ws.id)


# --- CHECK / UNIQUE constraints -----------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"page_start": 3, "page_end": 2}, "ck_paper_chunks_page_range_valid"),
        ({"page_start": 0, "page_end": 1}, "ck_paper_chunks_page_range_valid"),
        ({"token_count": 0}, "ck_paper_chunks_token_count_positive"),
        ({"chunk_index": -1}, "ck_paper_chunks_chunk_index_non_negative"),
    ],
)
async def test_chunk_check_constraints(db_session: AsyncSession, overrides: dict, constraint: str) -> None:
    ws = await _workspace(db_session, await _user(db_session))
    paper = await _paper(db_session, ws)
    await _expect_violation(db_session, _chunk(paper, **overrides), constraint)


async def test_duplicate_chunk_index_per_paper_rejected(db_session: AsyncSession) -> None:
    ws = await _workspace(db_session, await _user(db_session))
    paper = await _paper(db_session, ws)
    db_session.add(_chunk(paper, index=0))
    await db_session.flush()
    await _expect_violation(db_session, _chunk(paper, index=0), "uq_paper_chunks_paper_id_chunk_index")


async def test_section_page_range_checked(db_session: AsyncSession) -> None:
    ws = await _workspace(db_session, await _user(db_session))
    paper = await _paper(db_session, ws)
    bad = PaperSection(paper_id=paper.id, order_index=0, text="x", page_start=5, page_end=4)
    await _expect_violation(db_session, bad, "ck_paper_sections_page_range_valid")


async def test_section_type_checked(db_session: AsyncSession) -> None:
    ws = await _workspace(db_session, await _user(db_session))
    paper = await _paper(db_session, ws)
    bad = PaperSection(paper_id=paper.id, order_index=0, text="x", page_start=1, page_end=1, section_type="poem")
    await _expect_violation(db_session, bad, "ck_paper_sections_section_type_valid")


async def test_duplicate_paper_in_same_workspace_rejected_but_allowed_elsewhere(
    db_session: AsyncSession,
) -> None:
    owner = await _user(db_session)
    ws1 = await _workspace(db_session, owner, "one")
    ws2 = await _workspace(db_session, owner, "two")
    await _paper(db_session, ws1, "same-file")
    await _paper(db_session, ws2, "same-file")  # allowed: different workspace
    duplicate = Paper(
        workspace_id=ws1.id,
        original_filename="copy.pdf",
        storage_key=f"papers/{uuid.uuid4()}.pdf",
        sha256=_sha("same-file"),
        file_size_bytes=10,
    )
    await _expect_violation(db_session, duplicate, "uq_papers_workspace_id_sha256")


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"status": "done"}, "ck_papers_status_valid"),
        ({"sha256": "NOT-HEX"}, "ck_papers_sha256_hex"),
        ({"file_size_bytes": 0}, "ck_papers_file_size_positive"),
        ({"publication_year": 1500}, "ck_papers_publication_year_range"),
    ],
)
async def test_paper_check_constraints(db_session: AsyncSession, overrides: dict, constraint: str) -> None:
    ws = await _workspace(db_session, await _user(db_session))
    values = dict(
        workspace_id=ws.id,
        original_filename="x.pdf",
        storage_key=f"papers/{uuid.uuid4()}.pdf",
        sha256=_sha("x"),
        file_size_bytes=10,
    )
    values.update(overrides)
    await _expect_violation(db_session, Paper(**values), constraint)


async def test_email_must_be_lowercase_at_db_level(db_session: AsyncSession) -> None:
    await _expect_violation(
        db_session, User(email="Mixed@Example.com", hashed_password="x"), "ck_users_email_lowercase"
    )


async def test_workspace_name_unique_per_owner_case_insensitive(db_session: AsyncSession) -> None:
    owner = await _user(db_session)
    other = await _user(db_session, "other@example.com")
    await _workspace(db_session, owner, "Deep Learning")
    await _workspace(db_session, other, "Deep Learning")  # allowed: different owner
    await _expect_violation(
        db_session, Workspace(owner_id=owner.id, name="deep learning"), "uq_workspaces_owner_id_lower_name"
    )


async def test_blank_workspace_name_rejected(db_session: AsyncSession) -> None:
    owner = await _user(db_session)
    await _expect_violation(db_session, Workspace(owner_id=owner.id, name="   "), "ck_workspaces_name_not_blank")


# --- Cascades -----------------------------------------------------------------------


async def test_deleting_user_cascades_to_everything(db_session: AsyncSession) -> None:
    owner = await _user(db_session)
    survivor = await _user(db_session, "survivor@example.com")
    ws = await _workspace(db_session, owner)
    paper = await _paper(db_session, ws)
    section = await _section(db_session, paper)
    db_session.add(_chunk(paper, section_id=section.id))
    survivor_ws = await _workspace(db_session, survivor, "kept")
    await db_session.commit()

    await db_session.execute(text("DELETE FROM users WHERE id = :id"), {"id": owner.id})
    await db_session.commit()

    for model in (Paper, PaperSection, PaperChunk):
        assert (await db_session.execute(select(func.count()).select_from(model))).scalar_one() == 0
    remaining = (await db_session.execute(select(Workspace.id))).scalars().all()
    assert remaining == [survivor_ws.id]
