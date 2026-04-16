"""HTTP clients with retry logic for the Herald notification service.

Provides ``AIClient`` for AI extraction calls and ``NotifyClient`` for
notification delivery, both backed by *tenacity* exponential-backoff
retries and *asyncio.Semaphore* concurrency control.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from models import ExtractionError, NotificationPayload, ProviderError

logger = logging.getLogger("herald.client")

# ---------------------------------------------------------------------------
# System prompt sent to the AI extraction endpoint
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You are a structured data extractor. Extract notification details from the user message.\n"
    "Return ONLY a JSON object with exactly these keys: \"to\", \"message\", \"type\".\n"
    "- \"to\": email address or phone number\n"
    "- \"message\": the content to send\n"
    "- \"type\": \"email\" if destination is an email, \"sms\" if destination is a phone number\n"
    "No explanations, no markdown, no extra fields. Only the JSON object."
)


# ---------------------------------------------------------------------------
# Custom retry predicate — only retry on transient HTTP errors
# ---------------------------------------------------------------------------


def _is_retryable(exc: BaseException) -> bool:
    """Return ``True`` when *exc* is a transient error worth retrying.

    Retries on:
    * ``httpx.HTTPStatusError`` with status **429** or **500**
    * ``httpx.TimeoutException``

    All other exceptions (including 4xx errors that are not 429) are
    considered permanent and will **not** be retried.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 500)
    if isinstance(exc, httpx.TimeoutException):
        return True
    return False


# ---------------------------------------------------------------------------
# AIClient — calls /v1/ai/extract
# ---------------------------------------------------------------------------


class AIClient:
    """Client for the AI extraction endpoint.

    Uses *tenacity* for automatic retries with exponential back-off and an
    ``asyncio.Semaphore`` to cap the number of concurrent outbound requests.
    """

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        semaphore: asyncio.Semaphore,
        base_url: str = "http://localhost:3001",
        api_key: str = "test-dev-2026",
    ) -> None:
        self._http_client = http_client
        self._semaphore = semaphore
        self._base_url = base_url
        self._api_key = api_key

    async def extract(self, user_input: str) -> str:
        """Send *user_input* to the AI extraction endpoint and return the raw content.

        The semaphore is acquired **inside** the method so that each retry
        attempt independently waits for a slot, keeping back-pressure
        consistent under load.

        Returns:
            The ``content`` string from the first choice in the AI response.

        Raises:
            ExtractionError: When all retry attempts are exhausted or a
                permanent error occurs.
        """
        try:
            return await self._extract_with_retry(user_input)
        except Exception as exc:
            raise ExtractionError(str(exc)) from exc

    @retry(
        retry=retry_if_exception(_is_retryable),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
        stop=stop_after_attempt(3),
        before_sleep=before_sleep_log(logger, logging.WARNING),
    )
    async def _extract_with_retry(self, user_input: str) -> str:
        """Internal retry-wrapped call to the AI extraction endpoint."""
        async with self._semaphore:
            logger.debug("ai_extract_start user_input_length=%d", len(user_input))
            response = await self._http_client.post(
                f"{self._base_url}/v1/ai/extract",
                headers={"X-API-Key": self._api_key},
                json={
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_input},
                    ]
                },
            )
            response.raise_for_status()
            content: str = response.json()["choices"][0]["message"]["content"]
            logger.debug("ai_extract_ok content_length=%d", len(content))
            return content


# ---------------------------------------------------------------------------
# NotifyClient — calls /v1/notify
# ---------------------------------------------------------------------------


class NotifyClient:
    """Client for the notification delivery endpoint.

    Uses *tenacity* for automatic retries with exponential back-off and an
    ``asyncio.Semaphore`` to cap the number of concurrent outbound requests.
    """

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        semaphore: asyncio.Semaphore,
        base_url: str = "http://localhost:3001",
        api_key: str = "test-dev-2026",
    ) -> None:
        self._http_client = http_client
        self._semaphore = semaphore
        self._base_url = base_url
        self._api_key = api_key

    async def send(self, payload: NotificationPayload) -> dict:
        """Deliver a validated *payload* to the notification provider.

        The semaphore is acquired **inside** the method so that each retry
        attempt independently waits for a slot.

        Returns:
            The JSON response body from the provider.

        Raises:
            ProviderError: When all retry attempts are exhausted or a
                permanent error occurs.
        """
        try:
            return await self._send_with_retry(payload)
        except Exception as exc:
            raise ProviderError(str(exc)) from exc

    @retry(
        retry=retry_if_exception(_is_retryable),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
        stop=stop_after_attempt(3),
        before_sleep=before_sleep_log(logger, logging.WARNING),
    )
    async def _send_with_retry(self, payload: NotificationPayload) -> dict:
        """Internal retry-wrapped call to the notification endpoint."""
        async with self._semaphore:
            logger.debug("notify_send_start to=%s type=%s", payload.to, payload.type)
            response = await self._http_client.post(
                f"{self._base_url}/v1/notify",
                headers={"X-API-Key": self._api_key},
                json=payload.model_dump(),
            )
            response.raise_for_status()
            result: dict = response.json()
            logger.debug("notify_send_ok result=%s", result)
            return result
