from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class AccessContext(BaseModel):
    """Must be supplied by a trusted backend, never derived from chat text."""
    model_config=ConfigDict(extra='forbid')
    org_id: str | None = None
    site_id: str | None = None
    permissions: list[str] = Field(default_factory=list)
    can_escalate: bool = True


class Message(BaseModel):
    model_config=ConfigDict(extra='forbid')
    role: Literal['user','assistant']
    content: str = Field(min_length=1,max_length=4000)


class AskRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    question: str = Field(min_length=1,max_length=4000)
    history: list[Message] = Field(default_factory=list,max_length=20)
    context: AccessContext | None = None
    address_style: Literal['ты','вы'] | None = None


class Source(BaseModel):
    chunk_id: str
    chunk_ids: list[str] = Field(default_factory=list)
    document: str
    location_type: str
    location: int
    segment_id: str
    source_sha256: str
    source_path: str
    text: str
    citation_id: str
    char_start: int = 0
    char_end: int = 0


class RagResult(BaseModel):
    status: Literal['answered','needs_clarification','insufficient_evidence','service_error']
    answer: str
    sources: list[Source] = Field(default_factory=list)
    can_escalate: bool
    request_id: str
    error_code: str | None = None


class RagError(Exception):
    def __init__(self,code,message):
        self.code=code;super().__init__(message)
