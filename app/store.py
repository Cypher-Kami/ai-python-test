"""In-memory request store with state-machine transitions and per-request locks.

Provides :class:`RequestStore` which manages :class:`RequestRecord` instances,
enforces valid status transitions, and exposes lazy per-request
:class:`asyncio.Lock` objects so callers can guarantee at-most-once processing.
"""

from __future__ import annotations

import asyncio
import time
import uuid

from models import FailureCategory, RequestRecord, Status

# ---------------------------------------------------------------------------
# Valid state-machine transitions
# ---------------------------------------------------------------------------

VALID_TRANSITIONS: dict[Status, set[Status]] = {
    "queued": {"processing"},
    "processing": {"sent", "failed"},
    "sent": set(),
    "failed": set(),
}


# ---------------------------------------------------------------------------
# Request Store
# ---------------------------------------------------------------------------

class RequestStore:
    """In-memory store for notification requests.

    Each request is identified by a short hex ID and progresses through the
    state machine defined by :data:`VALID_TRANSITIONS`.  Per-request
    :class:`asyncio.Lock` objects are created lazily via :meth:`get_lock` so
    that callers can serialise mutations on a single request without blocking
    unrelated requests.
    """

    def __init__(self) -> None:
        self._requests: dict[str, RequestRecord] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # -- creation -----------------------------------------------------------

    def create(self, user_input: str) -> RequestRecord:
        """Create a new request with status ``queued``.

        Generates a 16-character hex ID from :func:`uuid.uuid4` and stores the
        record internally.

        Args:
            user_input: The raw natural-language instruction from the caller.

        Returns:
            The newly created :class:`RequestRecord`.
        """
        request_id = uuid.uuid4().hex[:16]
        record = RequestRecord(
            id=request_id,
            user_input=user_input,
            status="queued",
            created_at=time.time(),
        )
        self._requests[request_id] = record
        return record

    # -- lookup -------------------------------------------------------------

    def get(self, request_id: str) -> RequestRecord | None:
        """Return the record for *request_id*, or ``None`` if it does not exist."""
        return self._requests.get(request_id)

    # -- locking ------------------------------------------------------------

    def get_lock(self, request_id: str) -> asyncio.Lock:
        """Return the :class:`asyncio.Lock` for *request_id*, creating it lazily.

        The caller is responsible for acquiring and releasing the lock.  All
        state transitions for a given request should happen while the lock is
        held to ensure at-most-once processing semantics.
        """
        if request_id not in self._locks:
            self._locks[request_id] = asyncio.Lock()
        return self._locks[request_id]

    # -- state transitions --------------------------------------------------

    def transition(
        self,
        request_id: str,
        new_status: Status,
        failure_category: FailureCategory | None = None,
        failure_reason: str | None = None,
    ) -> bool:
        """Attempt to transition a request to *new_status*.

        The transition is validated against :data:`VALID_TRANSITIONS`.  If the
        transition is not permitted the record is left unchanged and ``False``
        is returned.

        When transitioning to ``failed``, the optional *failure_category* and
        *failure_reason* are stored on the record.  Locks for terminal states
        (``sent``, ``failed``) are cleaned up to avoid memory accumulation.

        .. note::

            This method **must** be called while the caller holds the
            per-request lock obtained from :meth:`get_lock`.

        Args:
            request_id: The ID of the request to transition.
            new_status: The desired target status.
            failure_category: Category of the failure (used when *new_status*
                is ``"failed"``).
            failure_reason: Human-readable failure description.

        Returns:
            ``True`` if the transition was applied, ``False`` otherwise.
        """
        record = self._requests.get(request_id)
        if record is None:
            return False

        allowed = VALID_TRANSITIONS[record.status]
        if new_status not in allowed:
            return False

        record.status = new_status
        if new_status == "failed":
            record.failure_category = failure_category
            record.failure_reason = failure_reason

        # Clean up lock for terminal states to avoid memory accumulation
        if new_status in {"sent", "failed"}:
            self._locks.pop(request_id, None)

        return True
