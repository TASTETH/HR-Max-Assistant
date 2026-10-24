"""Validated contracts for authenticated channel adapters and router handlers."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints, model_validator

ID = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)]
DbID = Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, protected_namespaces=())


class Route(StrEnum):
    DELIVERY = "delivery"
    ACTIVATE = "invitation.activate"
    ACTIVATE_REQUIRED = "activation_required"
    HELP = "help"
    IDENTITY = "assistant_identity"
    GREETING = "greeting"
    HR_MENU = "hr.menu"
    HR_CONTACT = "hr.contact"
    HR_REQUEST = "hr.request"
    HR_FOLLOWUP = "hr.followup"
    REGISTRATION = "registration.answer"
    PREFERENCE = "preference.address"
    PROFILE = "profile.submit"
    SURVEY_SHOW = "survey.show"
    SURVEY_ANSWER = "survey.answer"
    SURVEY_COMMENT = "survey.comment"
    SURVEY_TOGGLE = "survey.toggle"
    SURVEY_SUBMIT = "survey.submit"
    PLAN = "onboarding.status"
    CONTENT = "content.show"
    RAG = "knowledge.question"
    FEEDBACK = "feedback.analyze"
    MIXED = "feedback_then_help"
    CLARIFY = "clarification"
    RESOLVE = "clarification.resolve"
    STALE = "stale_action"
    UNSUPPORTED = "unsupported"


class Attachment(Model):
    kind: Literal["image", "document", "photo"]
    reference: Annotated[str, StringConstraints(min_length=1, max_length=2048)]
    name: str = Field(default="", max_length=256)


class IncomingEvent(Model):
    """Only an authenticated adapter may submit events, including delivery receipts."""

    provider_event_id: ID
    channel: Literal["max"] = "max"
    kind: Literal["text", "callback", "attachment", "invitation", "delivery"] = "text"
    account_id: ID | None = None
    chat_id: ID | None = None
    text: str = Field(default="", max_length=4000)
    callback_data: str | None = Field(default=None, max_length=128)
    callback_id: ID | None = None
    reply_to_message_id: ID | None = None
    attachments: list[Attachment] = Field(default_factory=list, max_length=5)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    external_message_id: ID | None = None
    delivered_at: datetime | None = None
    invitation_token: SecretStr | None = Field(default=None, exclude=True, repr=False)
    invitation_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$", repr=False)

    @model_validator(mode="after")
    def validate_event(self):
        if self.occurred_at.tzinfo is None or (self.delivered_at and self.delivered_at.tzinfo is None):
            raise ValueError("Event timestamps must include a timezone")
        if self.kind == "delivery":
            if not self.external_message_id or not self.delivered_at:
                raise ValueError("Delivery requires external_message_id and delivered_at")
        elif not self.account_id or not self.chat_id:
            raise ValueError("Employee events require account_id and chat_id")
        if self.kind == "callback" and not self.callback_data:
            raise ValueError("Callback requires callback_data")
        if self.kind == "text" and not self.text:
            raise ValueError("Text must not be empty")
        if self.kind == "attachment" and not self.attachments:
            raise ValueError("Attachment event requires attachments")
        if self.kind == "invitation" and not (self.invitation_token or self.invitation_hash):
            raise ValueError("Invitation requires a token or a normalized SHA-256 hash")
        return self

    @property
    def key(self) -> str:
        key = f"{self.channel}:{self.provider_event_id}"
        return key if len(key) <= 128 else f"{self.channel}:sha256:{hashlib.sha256(key.encode()).hexdigest()}"

    def envelope(self) -> dict:
        payload = self.model_dump(mode="json")
        if self.kind == "invitation":
            if self.invitation_token:
                payload["invitation_hash"] = hashlib.sha256(self.invitation_token.get_secret_value().encode()).hexdigest()
            payload["text"] = ""
        # Secrets in a /start deep link are removed before the durable inbox.
        match = re.fullmatch(r"/start(?:@\w+)?\s+(\S+)", self.text, re.IGNORECASE)
        if self.kind == "text" and match:
            payload.update(kind="invitation", text="", invitation_hash=hashlib.sha256(match[1].encode()).hexdigest())
        return payload


class Choice(Model):
    code: Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_-]{1,40}$")]
    label: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    comment_required: bool = False
    exclusive: bool = False
    next_event_id: DbID | None = None


class QuestionSettings(Model):
    """Choice questions use a feedback plan_event; typed answers live in router tables."""

    kind: Literal["rating", "feedback", "task", "choice", "multiselect"]
    survey_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    prompt: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    options: list[Choice] = Field(default_factory=list, max_length=20)
    next_event_id: DbID | None = None

    @model_validator(mode="after")
    def validate_options(self):
        if (self.kind in {"choice", "multiselect"}) != bool(self.options):
            raise ValueError("Choice questions require options; other types cannot have options")
        if len({o.code for o in self.options}) != len(self.options):
            raise ValueError("Choice codes must be unique")
        if len({o.label.casefold() for o in self.options}) != len(self.options):
            raise ValueError("Choice labels must be unique")
        if self.kind == "multiselect" and any(o.next_event_id for o in self.options):
            raise ValueError("Multiselect branches use the question-level next_event_id")
        return self


class Question(Model):
    id: DbID
    plan_id: DbID
    status: str
    deadline_at: datetime
    settings: QuestionSettings
    revision: int = 0
    stage: Literal["draft", "awaiting_comment", "completed"] = "draft"
    selected: list[str] = Field(default_factory=list)
    score: int | None = None
    root_message_id: int | None = None


class Action(Model):
    route: Route
    target_id: DbID | None = None
    revision: int | None = Field(default=None, ge=0)
    value: str = Field(default="", max_length=256)
    pending_id: str | None = None


class Context(Model):
    employee_id: DbID | None = None
    hr_id: DbID | None = None
    lifecycle: str = "unknown"
    registration_step: str = ""
    registration_revision: int = 0
    site_code: str | None = None
    org_unit_id: DbID | None = None
    preferred_address: Literal["formal", "informal"] = "formal"
    profile_revision: int = 0
    input_mode: dict = Field(default_factory=dict)
    questions: list[Question] = Field(default_factory=list)
    open_cases: list[int] = Field(default_factory=list)
    action: Action | None = None
    reply_question_id: int | None = None
    reply_case_id: int | None = None
    reply_known: bool = False
    action_invalid: bool = False

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()

    def question(self, target_id: int | None) -> Question | None:
        return next((q for q in self.questions if q.id == target_id), None)


class Intent(Model):
    name: Literal["question", "plan", "feedback", "mixed", "human", "greeting", "identity", "answer", "unknown"] = "unknown"


class Decision(Model):
    route: Route
    reason: str
    target_id: DbID | None = None
    revision: int | None = None
    value: str = ""
    pending_id: str | None = None


class Button(Model):
    label: str
    callback_data: str | None = None
    url: str | None = None


class OutgoingMessage(Model):
    message_id: int
    chat_id: str
    text: str
    buttons: list[Button] = Field(default_factory=list)
    attachments: list[Attachment] = Field(default_factory=list)


class RouterResult(Model):
    event_id: str
    status: Literal["pending", "processing", "done", "dead"]
    route: Route | None = None
    employee_id: int | None = None
    message_id: int | None = None
    case_id: int | None = None
    reason: str = ""
    notice: str = ""
    outgoing: list[OutgoingMessage] = Field(default_factory=list)
    job_ids: list[int] = Field(default_factory=list)
    duplicate: bool = False


class RagRequest(Model):
    message_id: int
    employee_id: int
    org_unit_id: int | None
    site_code: str | None
    question: str
    allowed_version_ids: list[int]


class RagAnswer(Model):
    found: bool
    text: str = Field(min_length=1, max_length=12000)
    chunk_ids: list[DbID] = Field(default_factory=list, max_length=20)
    model_version: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_sources(self):
        if self.found != bool(self.chunk_ids) or len(self.chunk_ids) != len(set(self.chunk_ids)):
            raise ValueError("A found answer requires distinct source chunks; not_found cannot cite chunks")
        return self


class RagBackend(Protocol):
    async def answer(self, request: RagRequest) -> RagAnswer: ...


class Sender(Protocol):
    async def send(self, message: OutgoingMessage) -> str:
        """Return the provider message ID; use message_id as the idempotency key."""
        ...


class RejectedBeforeAcceptance(RuntimeError):
    """The channel definitively rejected the send before accepting it."""


class ContextChanged(RuntimeError):
    pass


class RetryLater(RuntimeError):
    pass


class InvalidAction(ValueError):
    pass
