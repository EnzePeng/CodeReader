"""Public API and streaming-event schemas."""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, Type, Union
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class APIErrorDetail(BaseModel):
    code: str
    message: str
    details: Optional[Dict[str, Any]] = None


class APIError(BaseModel):
    error: APIErrorDetail


class ProjectOpenRequest(BaseModel):
    path: str = Field(min_length=1, max_length=32_767)


class ProjectOpenResponse(BaseModel):
    project_id: str
    name: str
    index_status: Dict[str, Any]


class ProjectPathRequest(BaseModel):
    project_id: str = Field(min_length=1)
    relative_path: str = Field(min_length=1, max_length=4096)


class ProjectRelativePathRequest(BaseModel):
    relative_path: str = Field(min_length=1)


class ExplainTarget(BaseModel):
    id: str = Field(min_length=1, max_length=256)
    mode: Optional[Literal["simple", "detailed"]] = None


class ExplainRequest(ProjectPathRequest):
    force: Union[Literal["none", "all"], List[str]] = "none"
    mode: Literal["simple", "detailed"] = "simple"
    targets: Optional[List[ExplainTarget]] = None
    job_id: Optional[str] = None


class Selection(BaseModel):
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=20_000)


class ChatRequest(ProjectPathRequest):
    question: str = Field(min_length=1, max_length=10_000)
    selection: Optional[Selection] = None
    history: List[ChatMessage] = Field(default_factory=list, max_length=50)
    job_id: Optional[str] = None


class Evidence(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    path: str
    start_line: int
    end_line: int
    content: str
    source_hash: str = ""
    language: str = "plaintext"
    relation: str = ""
    symbol: Optional[str] = None
    score: float = 0.0
    metadata: Dict[str, Any] = Field(default_factory=dict)


StreamType = Literal[
    "evidence", "delta", "status", "complete", "cancelled", "error"
]


class EvidencePayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    items: List[Evidence]


class DeltaPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    text: str
    target: str


class StatusPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    state: str
    message: Optional[str] = None


class CompletePayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    result: Optional[Dict[str, Any]] = None
    warnings: Optional[List[str]] = None


class CancelledPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    reason: Optional[str] = None


class ErrorPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    code: str
    message: str
    details: Optional[Dict[str, Any]] = None


_PAYLOAD_MODELS: Dict[StreamType, Type[BaseModel]] = {
    "evidence": EvidencePayload,
    "delta": DeltaPayload,
    "status": StatusPayload,
    "complete": CompletePayload,
    "cancelled": CancelledPayload,
    "error": ErrorPayload,
}


class StreamEnvelope(BaseModel):
    job_id: str
    seq: int = Field(ge=0)
    type: StreamType
    scope_id: str
    payload: Dict[str, Any]


class StreamSequence:
    """Monotonic envelope generator scoped to one request/job."""

    def __init__(self, *, job_id: Optional[str] = None, scope_id: str) -> None:
        self.job_id = job_id or uuid4().hex
        self.scope_id = scope_id
        self._seq = 0

    def event(self, event_type: StreamType, payload: Dict[str, Any]) -> StreamEnvelope:
        validated_payload = _PAYLOAD_MODELS[event_type].model_validate(payload)
        envelope = StreamEnvelope(
            job_id=self.job_id,
            seq=self._seq,
            type=event_type,
            scope_id=self.scope_id,
            payload=validated_payload.model_dump(exclude_none=True),
        )
        self._seq += 1
        return envelope
