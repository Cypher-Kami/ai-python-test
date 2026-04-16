"""FastAPI application and REST endpoints for the Herald notification service.

Defines the application lifespan (shared HTTP client, semaphores, store, and
service clients), and exposes three endpoints:

* ``POST /v1/requests`` - create a new notification request.
* ``POST /v1/requests/{request_id}/process`` - run the processing pipeline.
* ``GET  /v1/requests/{request_id}`` - query current request status.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException

from client import AIClient, NotifyClient
from models import CreateRequest, CreateResponse, StatusResponse
from services import process_request
from store import RequestStore

# ---------------------------------------------------------------------------
# Structured logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("herald.api")

# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------

PROVIDER_BASE_URL = "http://localhost:3001"
API_KEY = "test-dev-2026"
AI_SEMAPHORE_LIMIT = 10
NOTIFY_SEMAPHORE_LIMIT = 20
HTTP_TIMEOUT = 10.0
PIPELINE_TIMEOUT = 25.0


# ---------------------------------------------------------------------------
# Application lifespan - shared resources
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialise shared resources on startup and tear them down on shutdown.

    Creates a single ``httpx.AsyncClient`` with connection pooling, two
    ``asyncio.Semaphore`` instances (AI and Notify), a ``RequestStore``, and
    the ``AIClient`` / ``NotifyClient`` wrappers.  All are stored on
    ``app.state`` so that endpoint handlers can access them.
    """
    # -- startup ------------------------------------------------------------
    http_client = httpx.AsyncClient(timeout=HTTP_TIMEOUT)
    ai_semaphore = asyncio.Semaphore(AI_SEMAPHORE_LIMIT)
    notify_semaphore = asyncio.Semaphore(NOTIFY_SEMAPHORE_LIMIT)

    store = RequestStore()
    ai_client = AIClient(http_client, ai_semaphore, PROVIDER_BASE_URL, API_KEY)
    notify_client = NotifyClient(http_client, notify_semaphore, PROVIDER_BASE_URL, API_KEY)

    app.state.store = store
    app.state.ai_client = ai_client
    app.state.notify_client = notify_client

    logger.info("Herald API started - resources initialised")
    yield

    # -- shutdown -----------------------------------------------------------
    await http_client.aclose()
    logger.info("Herald API shutdown - HTTP client closed")


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

app = FastAPI(title="Herald - Intelligent Notification Service", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.post("/v1/requests", status_code=201, response_model=CreateResponse)
async def create_request(body: CreateRequest) -> CreateResponse:
    """Create a new notification request with status ``queued``.

    Accepts a JSON body with a ``user_input`` string and returns the
    generated request ID.
    """
    record = app.state.store.create(body.user_input)
    logger.info("request_id=%s status=queued", record.id)
    return CreateResponse(id=record.id)


@app.post("/v1/requests/{request_id}/process", response_model=StatusResponse)
async def process(request_id: str) -> StatusResponse:
    """Run the synchronous processing pipeline for *request_id*.

    The endpoint acquires a per-request lock to guarantee at-most-once
    processing.  If the request has already left the ``queued`` state the
    current status is returned immediately (idempotency).

    Returns:
        ``StatusResponse`` with the final status (``sent`` or ``failed``),
        or the current status if the request was already processed.

    Raises:
        HTTPException: 404 when *request_id* does not exist.
    """
    store: RequestStore = app.state.store
    ai_client: AIClient = app.state.ai_client
    notify_client: NotifyClient = app.state.notify_client

    # 1. Check existence
    record = store.get(request_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Request not found")

    # 2. Acquire per-request lock (serialises concurrent /process calls)
    lock = store.get_lock(request_id)
    async with lock:
        # Re-read after acquiring the lock - state may have changed
        record = store.get(request_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Request not found")

        # 3. Idempotency: if no longer queued, return current status
        if record.status != "queued":
            return StatusResponse(id=record.id, status=record.status)

        # 4. Transition queued → processing
        store.transition(request_id, "processing")
        logger.info("request_id=%s status=processing", request_id)

        # 5. Run the pipeline (handles sent/failed transitions internally)
        result = await process_request(
            request_id,
            record.user_input,
            store,
            ai_client,
            notify_client,
            PIPELINE_TIMEOUT,
        )
        return result


@app.get("/v1/requests/{request_id}", response_model=StatusResponse)
async def get_status(request_id: str) -> StatusResponse:
    """Return the current status of *request_id*.

    Raises:
        HTTPException: 404 when *request_id* does not exist.
    """
    record = app.state.store.get(request_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Request not found")
    return StatusResponse(id=record.id, status=record.status)
