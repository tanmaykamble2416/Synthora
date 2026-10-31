from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict

T = TypeVar("T")


class InputSchema(BaseModel):
    """Base for request bodies. extra='forbid' rejects unknown fields such as
    'id' or 'owner_id' (prevents mass assignment)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class OutputSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int
