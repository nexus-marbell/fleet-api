"""Server Ed25519 keypair management for callback signing.

On startup, generates an Ed25519 keypair (or loads from the
FLEET_SERVER_PRIVATE_KEY environment variable).  Exposes helpers
to sign outgoing callback requests so agents can verify them
against the server's public key published at GET /manifest.

Signing protocol mirrors request auth (RFC section 4.3):
  METHOD\nPATH\nTIMESTAMP\nSHA256(BODY)
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level singleton keypair
# ---------------------------------------------------------------------------

_private_key: Ed25519PrivateKey | None = None
_public_key: Ed25519PublicKey | None = None


def _load_or_generate_keypair() -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    """Load the server keypair from env or generate a fresh one.

    If ``FLEET_SERVER_PRIVATE_KEY`` is set, it should contain a
    PEM-encoded Ed25519 private key.  Otherwise a new keypair is
    generated (suitable for development / ephemeral deployments).
    """
    env_key = os.environ.get("FLEET_SERVER_PRIVATE_KEY")
    if env_key is not None:
        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        private_key = load_pem_private_key(env_key.encode(), password=None)
        if not isinstance(private_key, Ed25519PrivateKey):
            raise TypeError(
                "FLEET_SERVER_PRIVATE_KEY must be an Ed25519 private key, "
                f"got {type(private_key).__name__}"
            )
        logger.info("Loaded server Ed25519 keypair from FLEET_SERVER_PRIVATE_KEY")
        return private_key, private_key.public_key()

    private_key = Ed25519PrivateKey.generate()
    logger.info("Generated ephemeral server Ed25519 keypair")
    return private_key, private_key.public_key()


def _ensure_keypair() -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    """Lazily initialise the module-level keypair."""
    global _private_key, _public_key  # noqa: PLW0603
    if _private_key is None or _public_key is None:
        _private_key, _public_key = _load_or_generate_keypair()
    return _private_key, _public_key


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_server_private_key() -> Ed25519PrivateKey:
    """Return the server's Ed25519 private key (initialising if needed)."""
    priv, _ = _ensure_keypair()
    return priv


def get_server_public_key() -> Ed25519PublicKey:
    """Return the server's Ed25519 public key (initialising if needed)."""
    _, pub = _ensure_keypair()
    return pub


def get_server_public_key_pem() -> str:
    """Return the server's public key as a PEM string.

    This is the value published at ``GET /manifest`` under
    ``auth.server_public_key`` so agents can verify callback signatures.
    """
    pub = get_server_public_key()
    return pub.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()


def get_server_private_key_pem() -> str:
    """Return the server's private key as a PEM string (for testing/export)."""
    priv = get_server_private_key()
    return priv.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()


def sign_callback(method: str, path: str, timestamp: str, body: bytes) -> str:
    """Sign a callback request and return the base64-encoded signature.

    Signing string format (same as request auth, RFC section 4.3)::

        METHOD\\nPATH\\nTIMESTAMP\\nSHA256(BODY)

    Args:
        method: HTTP method (e.g. ``"POST"``).
        path: URL path of the callback endpoint.
        timestamp: ISO 8601 timestamp string.
        body: Raw request body bytes.

    Returns:
        Base64-encoded Ed25519 signature.
    """
    body_hash = hashlib.sha256(body).hexdigest()
    signing_string = f"{method}\n{path}\n{timestamp}\n{body_hash}".encode()
    priv = get_server_private_key()
    signature = priv.sign(signing_string)
    return base64.b64encode(signature).decode()


def reset_keypair() -> None:
    """Reset the cached keypair (for testing only)."""
    global _private_key, _public_key  # noqa: PLW0603
    _private_key = None
    _public_key = None
