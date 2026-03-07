"""Ed25519 request signing for outbound HTTP calls.

Implements the same signing protocol as fleet-api's auth middleware
(``fleet_api.middleware.auth``) so the sidecar's requests pass verification:

    Signing string: ``METHOD\\nPATH\\nTIMESTAMP\\nSHA256(BODY)``
    Header:         ``Authorization: Signature <agent_id>:<base64_signature>``
    Timestamp:      ``X-Fleet-Timestamp: <ISO 8601>``
"""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def sign_request(
    method: str,
    path: str,
    body: bytes,
    private_key: Ed25519PrivateKey,
    agent_id: str,
) -> dict[str, str]:
    """Produce ``Authorization`` and ``X-Fleet-Timestamp`` headers.

    Parameters
    ----------
    method:
        HTTP method (``GET``, ``POST``, etc.).
    path:
        Request path including query string (e.g. ``/agents/a1/tasks/pending``).
    body:
        Raw request body (``b""`` for GET requests).
    private_key:
        The agent's Ed25519 private key.
    agent_id:
        The agent's registered identifier.

    Returns
    -------
    dict[str, str]
        Headers dict with ``Authorization`` and ``X-Fleet-Timestamp``.
    """
    timestamp = datetime.now(UTC).isoformat()
    body_hash = hashlib.sha256(body).hexdigest()
    signing_string = f"{method}\n{path}\n{timestamp}\n{body_hash}".encode()

    signature = private_key.sign(signing_string)
    sig_b64 = base64.b64encode(signature).decode("utf-8")

    return {
        "Authorization": f"Signature {agent_id}:{sig_b64}",
        "X-Fleet-Timestamp": timestamp,
    }
