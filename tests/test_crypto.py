"""Tests for server Ed25519 keypair management (fleet_api.crypto).

Covers:
  - Keypair generation (ephemeral)
  - Loading from FLEET_SERVER_PRIVATE_KEY env var
  - Sign / verify round-trip
  - PEM export format
  - reset_keypair for test isolation
"""

from __future__ import annotations

import base64
import hashlib
import os
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)

from fleet_api.crypto import (
    get_server_private_key,
    get_server_private_key_pem,
    get_server_public_key,
    get_server_public_key_pem,
    reset_keypair,
    sign_callback,
)


@pytest.fixture(autouse=True)
def _isolate_keypair():
    """Reset the module-level keypair before and after each test."""
    reset_keypair()
    yield
    reset_keypair()


# ---------------------------------------------------------------------------
# Keypair generation
# ---------------------------------------------------------------------------


class TestKeypairGeneration:
    def test_generates_ed25519_private_key(self) -> None:
        """get_server_private_key returns an Ed25519PrivateKey."""
        key = get_server_private_key()
        assert isinstance(key, Ed25519PrivateKey)

    def test_generates_ed25519_public_key(self) -> None:
        """get_server_public_key returns an Ed25519PublicKey."""
        key = get_server_public_key()
        assert isinstance(key, Ed25519PublicKey)

    def test_keypair_is_consistent(self) -> None:
        """Private and public keys belong to the same keypair."""
        priv = get_server_private_key()
        pub = get_server_public_key()
        # Sign with private, verify with public
        msg = b"test message"
        sig = priv.sign(msg)
        pub.verify(sig, msg)  # raises if mismatch

    def test_keypair_is_cached(self) -> None:
        """Subsequent calls return the same key objects."""
        priv1 = get_server_private_key()
        priv2 = get_server_private_key()
        assert priv1 is priv2

        pub1 = get_server_public_key()
        pub2 = get_server_public_key()
        assert pub1 is pub2


# ---------------------------------------------------------------------------
# Loading from env var
# ---------------------------------------------------------------------------


class TestLoadFromEnv:
    def test_loads_private_key_from_env(self) -> None:
        """When FLEET_SERVER_PRIVATE_KEY is set, uses that key."""
        known_key = Ed25519PrivateKey.generate()
        pem = known_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()

        with patch.dict(os.environ, {"FLEET_SERVER_PRIVATE_KEY": pem}):
            loaded = get_server_private_key()

        # Same key — sign with original, verify with loaded's public key
        msg = b"round-trip"
        sig = known_key.sign(msg)
        loaded.public_key().verify(sig, msg)

    def test_env_key_wrong_type_raises(self) -> None:
        """Non-Ed25519 PEM in env var raises TypeError."""
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

        wrong_key = X25519PrivateKey.generate()
        pem = wrong_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()

        with patch.dict(os.environ, {"FLEET_SERVER_PRIVATE_KEY": pem}):
            with pytest.raises(TypeError, match="Ed25519"):
                get_server_private_key()


# ---------------------------------------------------------------------------
# Sign / verify round-trip
# ---------------------------------------------------------------------------


class TestSignCallback:
    def test_sign_produces_base64(self) -> None:
        """sign_callback returns a valid base64 string."""
        sig = sign_callback("POST", "/callback", "2026-03-07T12:00:00Z", b'{"ok": true}')
        # Should be valid base64
        decoded = base64.b64decode(sig)
        assert len(decoded) == 64  # Ed25519 signatures are 64 bytes

    def test_sign_verify_round_trip(self) -> None:
        """Signature produced by sign_callback verifies with the public key."""
        method = "POST"
        path = "/agent/callback"
        timestamp = "2026-03-07T14:30:00+00:00"
        body = b'{"task_id": "task-abc", "status": "completed"}'

        sig_b64 = sign_callback(method, path, timestamp, body)

        # Rebuild signing string the same way
        body_hash = hashlib.sha256(body).hexdigest()
        signing_string = f"{method}\n{path}\n{timestamp}\n{body_hash}".encode()

        pub = get_server_public_key()
        sig_bytes = base64.b64decode(sig_b64)
        pub.verify(sig_bytes, signing_string)  # raises on failure

    def test_different_body_produces_different_signature(self) -> None:
        """Changing the body produces a different signature."""
        sig1 = sign_callback("POST", "/cb", "2026-03-07T12:00:00Z", b'{"a": 1}')
        sig2 = sign_callback("POST", "/cb", "2026-03-07T12:00:00Z", b'{"a": 2}')
        assert sig1 != sig2

    def test_different_path_produces_different_signature(self) -> None:
        """Changing the path produces a different signature."""
        body = b'{"x": 1}'
        sig1 = sign_callback("POST", "/callback/a", "2026-03-07T12:00:00Z", body)
        sig2 = sign_callback("POST", "/callback/b", "2026-03-07T12:00:00Z", body)
        assert sig1 != sig2


# ---------------------------------------------------------------------------
# PEM export
# ---------------------------------------------------------------------------


class TestPemExport:
    def test_public_key_pem_format(self) -> None:
        """get_server_public_key_pem returns a PEM-encoded public key."""
        pem = get_server_public_key_pem()
        assert pem.startswith("-----BEGIN PUBLIC KEY-----")
        assert pem.strip().endswith("-----END PUBLIC KEY-----")

    def test_private_key_pem_format(self) -> None:
        """get_server_private_key_pem returns a PEM-encoded private key."""
        pem = get_server_private_key_pem()
        assert pem.startswith("-----BEGIN PRIVATE KEY-----")
        assert pem.strip().endswith("-----END PRIVATE KEY-----")

    def test_public_key_pem_is_loadable(self) -> None:
        """Exported PEM can be loaded back into a public key object."""
        from cryptography.hazmat.primitives.serialization import load_pem_public_key

        pem = get_server_public_key_pem()
        loaded = load_pem_public_key(pem.encode())
        assert isinstance(loaded, Ed25519PublicKey)

    def test_public_key_pem_matches_private_key(self) -> None:
        """Exported public PEM corresponds to the private key."""
        from cryptography.hazmat.primitives.serialization import load_pem_public_key

        pem = get_server_public_key_pem()
        loaded_pub = load_pem_public_key(pem.encode())

        priv = get_server_private_key()
        msg = b"pem-round-trip"
        sig = priv.sign(msg)
        loaded_pub.verify(sig, msg)
