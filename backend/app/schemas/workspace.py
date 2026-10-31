import uuid
from datetime import datetime
from typing import Annotated

from pydantic import StringConstraints, model_validator

from app.schemas.common import InputSchema, OutputSchema

WorkspaceName = Annotated[str, StringConstraints(min_length=1, max_length=200)]
WorkspaceDescription = Annotated[str, StringConstraints(max_length=5000)]


class WorkspaceCreate(InputSchema):
    name: WorkspaceName
    description: WorkspaceDescription | None = None


class WorkspaceUpdate(InputSchema):
    name: WorkspaceName | None = None
    description: WorkspaceDescription | None = None

    @model_validator(mode="after")
    def _check(self) -> "WorkspaceUpdate":
        if not self.model_fields_set:
            raise ValueError("Provide at least one field to update.")
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("name cannot be null.")
        return self


class WorkspaceRead(OutputSchema):
    id: uuid.UUID
    name: str
    description: str | None
    created_at: datetime
    updated_at: datetime
