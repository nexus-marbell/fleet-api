"""Ed25519 request signature verification.

Signing protocol: METHOD\\nPATH\\nTIMESTAMP\\nSHA256(BODY)

The require_auth dependency extracts the agent_id and Ed25519 signature
from the Authorization header, validates the X-Fleet-Timestamp against a
+-5 minute replay window, rebuilds the signing string from the request,
and verifies the signature against the agent's registered public key.

Agent lookup is abstracted behind the AgentLookup protocol so this module
has no dependency on the database models (Issue #7) or error middleware
(Issue #9).
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import Depends, HTTPException, Request

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPLAY_WINDOW = timedelta(minutes=5)

# Endpoints that bypass authentication (RFC §4.4).
# /agents/register is not listed in RFC §4.4 but must be unprotected:
# an agent cannot sign a registration request before it has registered a
# keypair — the bootstrap paradox.  Documented as a deliberate decision.
# / is the root path, conventionally the manifest or a redirect — kept
# unprotected for discovery consistency.
UNPROTECTED_PATHS: frozenset[str] = frozenset(
    {"/", "/manifest", "/agents/register", "/health", "/errors", "/openapi.json"}
)


# ---------------------------------------------------------------------------
# Data classes & protocols
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AuthenticatedAgent:
    """Represents a verified agent identity."""

    agent_id: str
    public_key: Ed25519PublicKey


class AgentLookup(Protocol):
    """Abstract interface for resolving agent credentials.

    Concrete implementations are injected via FastAPI dependency overrides.
    The placeholder :class:`PlaceholderAgentLookup` raises
    ``NotImplementedError``; it exists only so the application can start
    before the models package (Issue #7) is merged.
    """

    async def get_agent_public_key(self, agent_id: str) -> Ed25519PublicKey | None:
        """Return the agent's public key, or ``None`` if not registered."""
        ...

    async def is_agent_suspended(self, agent_id: str) -> bool:
        """Return ``True`` if the agent is suspended."""
        ...


class PlaceholderAgentLookup:
    """No-op implementation -- replaced when models merge (#7)."""

    async def get_agent_public_key(self, agent_id: str) -> Ed25519PublicKey | None:
        raise NotImplementedError(
            "AgentLookup not configured -- override get_agent_lookup dependency"
        )

    async def is_agent_suspended(self, agent_id: str) -> bool:
        raise NotImplementedError(
            "AgentLookup not configured -- override get_agent_lookup dependency"
        )


# ---------------------------------------------------------------------------
# Error helper
# ---------------------------------------------------------------------------


def auth_error(code: str, message: str, status_code: int = 401) -> HTTPException:
    """Create a standardised auth error response.

    Uses plain ``HTTPException`` so this module does not depend on the
    error middleware (Issue #9).
    """
    return HTTPException(
        status_code=status_code,
        detail={"error": {"code": code, "message": message}},
    )


# ---------------------------------------------------------------------------
# Signing / verification primitives
# ---------------------------------------------------------------------------


def build_signing_string(
    method: str, path: str, timestamp: str, body: bytes
) -> bytes:
    r"""Construct the signing string: ``METHOD\nPATH\nTIMESTAMP\nSHA256(BODY)``."""
    body_hash = hashlib.sha256(body).hexdigest()
    signing_string = f"{method}\n{path}\n{timestamp}\n{body_hash}"
    return signing_string.encode()


def verify_signature(
    public_key: Ed25519PublicKey,
    signing_string: bytes,
    signature: bytes,
) -> bool:
    """Verify an Ed25519 signature.

    Returns ``True`` if valid, ``False`` on :class:`InvalidSignature`.
    """
    try:
        public_key.verify(signature, signing_string)
        return True
    except InvalidSignature:
        return False


# ---------------------------------------------------------------------------
# Header / timestamp parsing
# ---------------------------------------------------------------------------


def parse_authorization_header(header: str) -> tuple[str, bytes]:
    """Parse ``Signature agent_id:base64_signature``.

    Returns ``(agent_id, signature_bytes)``.
    Raises :class:`ValueError` on malformed input.
    """
    if not header.startswith("Signature "):
        raise ValueError("Authorization header must start with 'Signature '")

    payload = header[len("Signature "):]
    if ":" not in payload:
        raise ValueError("Authorization header must contain 'agent_id:signature'")

    agent_id, sig_b64 = payload.split(":", 1)
    if not agent_id or not sig_b64:
        raise ValueError("Empty agent_id or signature")

    signature = base64.b64decode(sig_b64)
    return agent_id, signature


def validate_timestamp(timestamp_str: str) -> datetime:
    """Validate *X-Fleet-Timestamp* is within the replay window.

    Returns the parsed :class:`datetime`.
    Raises :class:`ValueError` if expired or malformed.
    """
    try:
        ts = datetime.fromisoformat(timestamp_str)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
    except (ValueError, TypeError) as e:
        raise ValueError(f"Invalid timestamp format: {timestamp_str}") from e

    now = datetime.now(UTC)
    if abs(now - ts) > REPLAY_WINDOW:
        raise ValueError(
            f"Timestamp {timestamp_str} is outside the \u00b15 minute replay window"
        )
    return ts


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------


async def get_agent_lookup() -> AgentLookup:
    """Default dependency -- returns the placeholder.

    Override this in ``app.dependency_overrides`` to inject a real
    implementation (or a test mock).
    """
    return PlaceholderAgentLookup()


async def require_auth(
    request: Request,
    lookup: AgentLookup = Depends(get_agent_lookup),
) -> AuthenticatedAgent | None:
    """FastAPI dependency that enforces Ed25519 request signing.

    Returns :class:`AuthenticatedAgent` for protected routes, or ``None``
    for unprotected paths.

    Error codes:

    * ``INVALID_SIGNATURE`` (401) -- malformed/missing header or bad signature
    * ``TIMESTAMP_EXPIRED`` (401) -- timestamp outside replay window
    * ``AGENT_NOT_REGISTERED`` (401) -- agent_id not found
    * ``NOT_AUTHORIZED`` (403) -- agent suspended
    * ``SERVICE_UNAVAILABLE`` (503) -- agent lookup not configured
    """
    # 1. Skip unprotected paths
    if request.url.path in UNPROTECTED_PATHS:
        return None

    # 2. Extract Authorization header
    auth_header = request.headers.get("Authorization")
    if auth_header is None:
        raise auth_error("INVALID_SIGNATURE", "Missing Authorization header")

    try:
        agent_id, signature = parse_authorization_header(auth_header)
    except ValueError as e:
        raise auth_error("INVALID_SIGNATURE", str(e)) from e

    # 3. Validate timestamp
    timestamp_str = request.headers.get("X-Fleet-Timestamp")
    if timestamp_str is None:
        # Missing header is a malformed request, not an expired timestamp.
        # TIMESTAMP_EXPIRED is reserved for timestamps that parse but fall
        # outside the ±5 min replay window (RFC §8).
        raise auth_error("INVALID_SIGNATURE", "Missing X-Fleet-Timestamp header")

    try:
        validate_timestamp(timestamp_str)
    except ValueError as e:
        raise auth_error("TIMESTAMP_EXPIRED", str(e)) from e

    # 4. Look up agent
    try:
        public_key = await lookup.get_agent_public_key(agent_id)
    except NotImplementedError:
        raise auth_error(
            "SERVICE_UNAVAILABLE",
            "Agent lookup is not configured",
            status_code=503,
        )
    if public_key is None:
        raise auth_error(
            "AGENT_NOT_REGISTERED", f"Agent '{agent_id}' is not registered"
        )

    # 5. Check suspension
    try:
        suspended = await lookup.is_agent_suspended(agent_id)
    except NotImplementedError:
        raise auth_error(
            "SERVICE_UNAVAILABLE",
            "Agent lookup is not configured",
            status_code=503,
        )
    if suspended:
        raise auth_error(
            "NOT_AUTHORIZED",
            f"Agent '{agent_id}' is suspended",
            status_code=403,
        )

    # 6. Build signing string and verify
    body = await request.body()
    # RFC §4.2: PATH includes query string (e.g., /workflows?status=active)
    path = request.url.path
    if request.url.query:
        path = f"{path}?{request.url.query}"
    signing_string = build_signing_string(
        method=request.method,
        path=path,
        timestamp=timestamp_str,
        body=body,
    )

    if not verify_signature(public_key, signing_string, signature):
        raise auth_error("INVALID_SIGNATURE", "Signature verification failed")

    return AuthenticatedAgent(agent_id=agent_id, public_key=public_key)
