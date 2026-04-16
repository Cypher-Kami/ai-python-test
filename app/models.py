"""Pydantic models and custom exceptions for the Herald notification service.

Defines API request/response schemas, internal data models, and the
GuardrailsError exception used throughout the processing pipeline.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Shared type aliases (single source of truth for the whole app)
# ---------------------------------------------------------------------------

Status = Literal["queued", "processing", "sent", "failed"]
FailureCategory = Literal[
    "extraction_error",
    "parsing_error",
    "validation_error",
    "provider_error",
    "timeout_error",
]


# ---------------------------------------------------------------------------
# Strict base for API schemas (rejects unexpected fields)
# ---------------------------------------------------------------------------

class _APISchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# API request / response models
# ---------------------------------------------------------------------------

class CreateRequest(_APISchema):
    """Body for POST /v1/requests."""

    user_input: str = Field(min_length=1)


class CreateResponse(_APISchema):
    """Response for POST /v1/requests."""

    id: str


class StatusResponse(_APISchema):
    """Response for GET /v1/requests/{id} and POST /v1/requests/{id}/process."""

    id: str
    status: Status


# ---------------------------------------------------------------------------
# Internal models
# ---------------------------------------------------------------------------

class NotificationPayload(BaseModel):
    """Validated payload sent to the notification provider."""

    to: str
    message: str
    type: Literal["email", "sms"]


class RequestRecord(BaseModel):
    """Internal record stored in the Request Store."""

    id: str
    user_input: str
    status: Status = "queued"
    failure_category: FailureCategory | None = None
    failure_reason: str | None = None
    created_at: float


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

GuardrailsCategory = Literal["parsing_error", "validation_error"]


class GuardrailsError(Exception):
    """Raised when the guardrails pipeline fails to produce a valid payload.

    Attributes:
        category: Either ``"parsing_error"`` or ``"validation_error"``.
        reason: Human-readable description of what went wrong.
    """

    def __init__(self, category: GuardrailsCategory, reason: str) -> None:
        self.category = category
        self.reason = reason
        super().__init__(reason)
