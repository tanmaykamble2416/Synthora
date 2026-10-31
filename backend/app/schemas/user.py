import uuid
from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator, EmailStr, Field, StringConstraints, model_validator

from app.schemas.common import InputSchema, OutputSchema

PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 128


def _lowercase(value: str) -> str:
    return value.lower()


NormalizedEmail = Annotated[EmailStr, Field(max_length=320), AfterValidator(_lowercase)]
# Passwords are never whitespace-stripped (spaces can be part of a passphrase).
Password = Annotated[
    str, StringConstraints(min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH, strip_whitespace=False)
]
FullName = Annotated[str, StringConstraints(min_length=1, max_length=200)]


class UserCreate(InputSchema):
    email: NormalizedEmail
    password: Password
    full_name: FullName | None = None


class UserUpdate(InputSchema):
    full_name: FullName | None = None
    password: Password | None = None
    current_password: Annotated[str, StringConstraints(max_length=PASSWORD_MAX_LENGTH, strip_whitespace=False)] | None = None

    @model_validator(mode="after")
    def _check(self) -> "UserUpdate":
        if not self.model_fields_set - {"current_password"}:
            raise ValueError("Provide at least one field to update.")
        if self.password is not None and not self.current_password:
            raise ValueError("current_password is required to change the password.")
        return self


class UserRead(OutputSchema):
    """Public representation. Deliberately has no password/hash field."""

    id: uuid.UUID
    email: str
    full_name: str | None
    is_active: bool
    created_at: datetime
