"""Test helpers for Ed25519 request signing.

Provides keypair generation and a ``sign_request()`` utility that produces
the ``Authorization`` and ``X-Fleet-Timestamp`` headers expected by
:func:`fleet_api.middleware.auth.require_auth`.
"""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


def generate_test_keypair() -> tuple[Ed25519PrivateKey, bytes]:
    """Generate a test Ed25519 keypair.

    Returns ``(private_key, public_key_pem_bytes)``.
    """
    private_key = Ed25519PrivateKey.generate()
    public_key_pem = private_key.public_key().public_bytes(
        Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
    )
    return private_key, public_key_pem


def sign_request(
    method: str,
    path: str,
    body: bytes | None,
    private_key: Ed25519PrivateKey,
    agent_id: str,
    timestamp: datetime | None = None,
) -> dict[str, str]:
    """Generate signed request headers for testing.

    Returns a dict containing ``Authorization`` and ``X-Fleet-Timestamp``
    headers that will satisfy :func:`~fleet_api.middleware.auth.require_auth`.
    """
    if timestamp is None:
        timestamp = datetime.now(UTC)

    ts_str = timestamp.isoformat()
    body_bytes = body or b""
    body_hash = hashlib.sha256(body_bytes).hexdigest()
    signing_string = f"{method}\n{path}\n{ts_str}\n{body_hash}".encode()

    signature = private_key.sign(signing_string)
    sig_b64 = base64.b64encode(signature).decode("utf-8")

    return {
        "Authorization": f"Signature {agent_id}:{sig_b64}",
        "X-Fleet-Timestamp": ts_str,
    }
