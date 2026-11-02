"""Pydantic models for FlowGate (backend/app/models.py)."""

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class RunCreate(BaseModel):
    project: str
    tenant: Optional[str] = None
    clearance: int = Field(ge=0, le=2)
    effective_level: int = Field(default=0, ge=0, le=2)
    effective_compartments: List[str] = Field(default_factory=list)
    parent_run_id: Optional[str] = None
    spawn_mode: Optional[Literal["clean", "continuing"]] = None


class ToolCallRequest(BaseModel):
    operation: Literal["read", "write"]
    resource_id: str
    content: Optional[str] = None
    request_id: Optional[str] = Field(
        default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
    )


class ChallengeRequest(BaseModel):
    content: str = Field(min_length=1, max_length=2000)
    writer: Literal["fresh", "tainted"]


class ArtifactCreate(BaseModel):
    resource_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-_]*$")
    kind: Literal["doc", "kv"]
    content: str = ""
