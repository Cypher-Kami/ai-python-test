"""Pipeline orchestration for the Herald notification service.

Contains :func:`process_request`, which drives the full processing pipeline:
AI extraction → guardrails parsing → notification delivery.  The pipeline is
wrapped in :func:`asyncio.wait_for` to enforce a global timeout and classifies
every failure into a structured :data:`~models.FailureCategory`.
"""

from __future__ import annotations

import asyncio
import logging
import time

from client import AIClient, NotifyClient
from guardrails import parse_llm_response
from models import ExtractionError, GuardrailsError, ProviderError, StatusResponse
from store import RequestStore

logger = logging.getLogger("herald.services")


async def process_request(
    request_id: str,
    user_input: str,
    store: RequestStore,
    ai_client: AIClient,
    notify_client: NotifyClient,
    pipeline_timeout: float = 25.0,
) -> StatusResponse:
    """Run the full processing pipeline for a single request.

    The caller is responsible for acquiring the per-request lock and
    transitioning the request from ``queued`` to ``processing`` **before**
    invoking this function.  This function handles the ``processing`` →
    ``sent`` or ``processing`` → ``failed`` transitions.

    Pipeline steps (inside *asyncio.wait_for*):
        1. Call :meth:`AIClient.extract` to obtain raw LLM output.
        2. Pass the raw output through :func:`guardrails.parse_llm_response`.
        3. Deliver the validated payload via :meth:`NotifyClient.send`.

    Args:
        request_id: Unique identifier of the request being processed.
        user_input: The raw natural-language instruction from the user.
        store: The in-memory request store for state transitions.
        ai_client: Client for the AI extraction endpoint.
        notify_client: Client for the notification delivery endpoint.
        pipeline_timeout: Maximum seconds for the entire pipeline.

    Returns:
        A :class:`StatusResponse` reflecting the final state of the request
        (either ``sent`` or ``failed``).
    """
    pipeline_start = time.time()

    async def _pipeline() -> StatusResponse:
        # --- Phase 1: AI extraction ---
        logger.info("request_id=%s phase=extract_start", request_id)
        t0 = time.time()
        raw_content = await ai_client.extract(user_input)
        t1 = time.time()
        logger.info(
            "request_id=%s phase=extract_done duration_ms=%.0f",
            request_id,
            (t1 - t0) * 1000,
        )

        # --- Phase 2: Guardrails ---
        t0 = time.time()
        payload = parse_llm_response(raw_content)
        t1 = time.time()
        logger.info(
            "request_id=%s phase=guardrails_done duration_ms=%.0f to=%s type=%s",
            request_id,
            (t1 - t0) * 1000,
            payload.to,
            payload.type,
        )

        # --- Phase 3: Notification delivery ---
        t0 = time.time()
        await notify_client.send(payload)
        t1 = time.time()
        logger.info(
            "request_id=%s phase=notify_done duration_ms=%.0f",
            request_id,
            (t1 - t0) * 1000,
        )

        # --- Success ---
        store.transition(request_id, "sent")
        total_ms = (time.time() - pipeline_start) * 1000
        logger.info(
            "request_id=%s status=sent total_duration_ms=%.0f",
            request_id,
            total_ms,
        )
        return StatusResponse(id=request_id, status="sent")

    try:
        return await asyncio.wait_for(_pipeline(), timeout=pipeline_timeout)

    except asyncio.TimeoutError:
        category = "timeout_error"
        reason = f"Pipeline timeout exceeded ({pipeline_timeout}s)"

    except ExtractionError as exc:
        category = "extraction_error"
        reason = str(exc)

    except GuardrailsError as exc:
        category = exc.category
        reason = exc.reason

    except ProviderError as exc:
        category = "provider_error"
        reason = str(exc)

    except Exception as exc:
        # Unexpected error — log at error level for investigation
        logger.error(
            "request_id=%s unexpected_error=%s", request_id, exc, exc_info=True
        )
        category = "extraction_error"
        reason = f"Unexpected: {exc}"

    # --- Failure path (all caught exceptions land here) ---
    store.transition(
        request_id,
        "failed",
        failure_category=category,
        failure_reason=reason,
    )
    logger.warning(
        "request_id=%s status=failed category=%s reason=%s",
        request_id,
        category,
        reason,
    )
    return StatusResponse(id=request_id, status="failed")
