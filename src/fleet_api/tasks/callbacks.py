"""Callback delivery — POST task results to the caller's callback_url.

When a task reaches a terminal state (completed, failed, cancelled,
retasked, redirected) and has a ``callback_url``, Fleet API POSTs the
task result signed with its Ed25519 key so the caller can verify
authenticity.

Signing protocol (RFC section 4.3):
  POST\n<CALLBACK_PATH>\n<TIMESTAMP>\n<SHA256(BODY)>

Headers sent on the callback:
  - Content-Type: application/json
  - X-Fleet-Signature: <base64 Ed25519 signature>
  - X-Fleet-Timestamp: <ISO 8601 timestamp>
  - X-Fleet-Key-Id: fleet-api
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from urllib.parse import urlparse

import httpx

from fleet_api.crypto import sign_callback
from fleet_api.tasks.models import Task

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RETRY_DELAYS = (1.0, 2.0, 4.0)  # Exponential backoff: 1s, 2s, 4s
MAX_ATTEMPTS = len(RETRY_DELAYS) + 1  # 1 initial + 3 retries


# ---------------------------------------------------------------------------
# Callback payload builder
# ---------------------------------------------------------------------------


def build_callback_payload(task: Task) -> dict:
    """Build the JSON payload sent to the callback_url.

    Contains the task's terminal state, result, and metadata
    needed by the caller to process the outcome.
    """
    status_value = task.status.value if hasattr(task.status, "value") else str(task.status)

    payload: dict = {
        "task_id": task.id,
        "workflow_id": task.workflow_id,
        "status": status_value,
        "result": task.result,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
    }
    return payload


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


async def deliver_callback(task: Task) -> bool:
    """Deliver a signed callback POST to the task's callback_url.

    Returns ``True`` if the callback was delivered successfully (2xx),
    ``False`` otherwise.  Never raises — failures are logged but do not
    propagate to the caller.

    If ``task.callback_url`` is ``None``, returns ``True`` immediately
    (no-op).
    """
    if task.callback_url is None:
        return True

    payload = build_callback_payload(task)
    body = json.dumps(payload).encode()

    parsed = urlparse(task.callback_url)
    path = parsed.path or "/"

    timestamp = datetime.now(UTC).isoformat()
    signature = sign_callback("POST", path, timestamp, body)

    headers = {
        "Content-Type": "application/json",
        "X-Fleet-Signature": signature,
        "X-Fleet-Timestamp": timestamp,
        "X-Fleet-Key-Id": "fleet-api",
    }

    for attempt in range(MAX_ATTEMPTS):
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    task.callback_url,
                    content=body,
                    headers=headers,
                )
            if 200 <= response.status_code < 300:
                logger.info(
                    "Callback delivered for task '%s' to %s (status %d)",
                    task.id,
                    task.callback_url,
                    response.status_code,
                )
                return True

            logger.warning(
                "Callback for task '%s' to %s returned %d (attempt %d/%d)",
                task.id,
                task.callback_url,
                response.status_code,
                attempt + 1,
                MAX_ATTEMPTS,
            )
        except Exception:
            logger.warning(
                "Callback for task '%s' to %s failed (attempt %d/%d)",
                task.id,
                task.callback_url,
                attempt + 1,
                MAX_ATTEMPTS,
                exc_info=True,
            )

        # Retry with backoff (skip sleep after last attempt)
        if attempt < len(RETRY_DELAYS):
            await asyncio.sleep(RETRY_DELAYS[attempt])

    logger.error(
        "Callback delivery failed for task '%s' to %s after %d attempts",
        task.id,
        task.callback_url,
        MAX_ATTEMPTS,
    )
    return False


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------


def schedule_callback(task: Task) -> asyncio.Task | None:
    """Schedule callback delivery as a background asyncio task.

    Returns the :class:`asyncio.Task` if a callback was scheduled,
    or ``None`` if the task has no ``callback_url``.

    Called from :func:`process_sidecar_event` when a terminal event
    is received.
    """
    if task.callback_url is None:
        return None

    bg_task = asyncio.create_task(
        deliver_callback(task),
        name=f"callback-{task.id}",
    )
    logger.info("Scheduled callback delivery for task '%s'", task.id)
    return bg_task
