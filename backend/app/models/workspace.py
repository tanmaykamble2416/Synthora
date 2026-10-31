import uuid
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.paper import Paper
    from app.models.user import User


class Workspace(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "workspaces"

    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    owner: Mapped["User"] = relationship(back_populates="workspaces", lazy="raise")
    papers: Mapped[list["Paper"]] = relationship(
        back_populates="workspace", passive_deletes=True, lazy="raise"
    )

    __table_args__ = (
        CheckConstraint("length(btrim(name)) > 0", name="name_not_blank"),
        # Case-insensitive unique name per owner. owner_id is the leading column,
        # so this index also serves "list my workspaces".
        Index("uq_workspaces_owner_id_lower_name", "owner_id", func.lower(name), unique=True),
    )

    def __repr__(self) -> str:
        return f"<Workspace id={self.id}>"
