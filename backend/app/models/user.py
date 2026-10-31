from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, String, true
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.workspace import Workspace


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        # Emails are normalised to lower case in the schema layer; the DB guarantees it,
        # which makes the plain UNIQUE constraint effectively case-insensitive.
        CheckConstraint("email = lower(email)", name="email_lowercase"),
    )

    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=true())

    workspaces: Mapped[list["Workspace"]] = relationship(
        back_populates="owner", passive_deletes=True, lazy="raise"
    )

    def __repr__(self) -> str:  # never include the password hash
        return f"<User id={self.id}>"
