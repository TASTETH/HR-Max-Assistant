"""Public HRInput/HRResult contracts and internal LLM schema (v1)."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints, model_validator

Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]

Source = Literal["survey", "message", "hr_request"]
Sentiment = Literal["positive", "neutral", "negative", "mixed", "unknown"]
Priority = Literal["low", "medium", "high"]
SignalName = Literal[
    "unclear_tasks", "poor_communication", "access_problem", "intent_to_leave",
    "overload", "lack_of_support", "conflict", "harassment", "hr_request",
]
TicketCategory = Literal[
    "unclear_tasks", "poor_communication", "access_problem", "intent_to_leave",
    "overload", "lack_of_support", "conflict", "harassment", "hr_request", "general_hr_request",
]
ErrorCode = Literal[
    "configuration_error", "ollama_unavailable", "ollama_timeout",
    "ollama_http_error", "ollama_invalid_response", "invalid_llm_output",
]

class Schema(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

class HRInput(Schema):
    """One incoming event from Router or Survey Engine, no platform-specific fields."""
    event_id: Identifier
    employee_id: Identifier
    source: Source
    text: str = Field(default="", max_length=4000)
    survey_id: Identifier | None = None
    question_id: Identifier | None = None
    question: ShortText | None = None  # context for short replies like "нет"
    score: StrictInt | None = Field(default=None, ge=1, le=10)
    created_at: datetime | None = None

    @model_validator(mode="after")
    def validate_source(self) -> HRInput:
        self.text = self.text.strip()
        if self.source == "survey":
            if self.survey_id is None or self.question_id is None:
                raise ValueError("Survey requires survey_id and question_id")
            if not self.text and self.score is None:
                raise ValueError("Survey requires text or score")
        else:
            if self.survey_id is not None or self.question_id is not None or self.question is not None or self.score is not None:
                raise ValueError("Survey fields are allowed only when source=survey")
            if self.source == "message" and not self.text:
                raise ValueError("Message requires text")
        if len(self.model_dump_json()) > 8000:
            raise ValueError("HRInput is too large")
        return self

class Signal(Schema):
    category: SignalName
    quote: Text  # MUST occur in HRInput.text

class LLMAnalysis(Schema):
    """The LLM classifies only; it never chooses priority or creates tickets."""
    sentiment: Sentiment
    summary: ShortText
    signals: list[Signal] = Field(default_factory=list, max_length=9)

class Analysis(Schema):
    sentiment: Sentiment
    summary: ShortText
    signals: list[Signal] = Field(default_factory=list, max_length=9)
    priority: Priority
    requires_hr: bool

class TicketDraft(Schema):
    """Proposal for developer #5; no DB ID and no side effects."""
    category: TicketCategory
    description: Text
    priority: Priority
    status: Literal["new"] = "new"

class AnalysisError(Schema):
    code: ErrorCode
    message: ShortText

class HRResult(Schema):
    event_id: Identifier
    employee_id: Identifier
    status: Literal["ok", "no_data", "error"]
    analysis: Analysis | None = None
    ticket: TicketDraft | None = None
    error: AnalysisError | None = None

    @model_validator(mode="after")
    def check_consistency(self) -> HRResult:
        if self.status == "ok":
            if self.analysis is None or self.error is not None:
                raise ValueError("ok requires analysis and cannot include error")
            if (self.ticket is not None) != self.analysis.requires_hr:
                raise ValueError("ticket presence must agree with requires_hr")
        else:
            if self.analysis is not None:
                raise ValueError("no_data/error cannot contain analysis")
            if (self.status == "error") != (self.error is not None):
                raise ValueError("error payload required only for status=error")
        return self
