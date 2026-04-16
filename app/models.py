"""Pydantic models and custom exceptions for the Herald notification service.

Defines API request/response schemas, internal data models, and the
GuardrailsError exception used throughout the processing pipeline.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# API request / response models
# ---------------------------------------------------------------------------

class CreateRequest(BaseModel):
    """Body for POST /v1/requests."""

    user_input: str


class CreateResponse(BaseModel):
    """Response for POST /v1/requests."""

    id: str


class StatusResponse(BaseModel):
    """Response for GET /v1/requests/{id} and POST /v1/requests/{id}/process."""

    id: str
    status: str


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
    status: Literal["queued", "processing", "sent", "failed"] = "queued"
    failure_category: (
        Literal[
            "extraction_error",
            "parsing_error",
            "validation_error",
            "provider_error",
            "timeout_error",
        ]
        | None
    ) = None
    failure_reason: str | None = None
    created_at: float


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class GuardrailsError(Exception):
    """Raised when the guardrails pipeline fails to produce a valid payload.

    Attributes:
        category: Either ``"parsing_error"`` or ``"validation_error"``.
        reason: Human-readable description of what went wrong.
    """

    def __init__(self, category: str, reason: str) -> None:
        self.category = category
        self.reason = reason
        super().__init__(reason)
