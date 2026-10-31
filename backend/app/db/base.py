"""Declarative Base, constraint naming convention and shared column mixins."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, MetaData, Uuid, func, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Every constraint/index gets a deterministic name. This lets migrations drop
# constraints by name and lets services map IntegrityErrors to precise messages.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    # Fetch server-generated values (timestamps, ids) with RETURNING after INSERT/UPDATE,
    # so async code never triggers an implicit lazy refresh.
    __mapper_args__ = {"eager_defaults": True}


class UUIDPrimaryKeyMixin:
    # Python default for ORM inserts; server default for raw SQL inserts.
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # onupdate is applied by SQLAlchemy (ORM and Core UPDATE statements), not by a DB trigger.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
