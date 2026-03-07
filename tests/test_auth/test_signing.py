"""Unit tests for Ed25519 signing primitives and header parsing.

All tests are pure unit tests -- no database, no HTTP server.
"""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from fleet_api.middleware.auth import (
    build_signing_string,
    parse_authorization_header,
    validate_timestamp,
    verify_signature,
)
from tests.auth_helpers import generate_test_keypair, sign_request

# ---------------------------------------------------------------------------
# build_signing_string
# ---------------------------------------------------------------------------


class TestBuildSigningString:
    def test_correct_format(self) -> None:
        """Signing string uses METHOD\\nPATH\\nTIMESTAMP\\nSHA256(BODY)."""
        body = b'{"task": "test"}'
        result = build_signing_string("POST", "/tasks", "2026-03-07T12:00:00+00:00", body)

        body_hash = hashlib.sha256(body).hexdigest()
        expected = f"POST\n/tasks\n2026-03-07T12:00:00+00:00\n{body_hash}".encode()
        assert result == expected

    def test_empty_body(self) -> None:
        """Empty body uses SHA256 of empty bytes."""
        result = build_signing_string("GET", "/agents", "2026-03-07T12:00:00+00:00", b"")

        empty_hash = hashlib.sha256(b"").hexdigest()
        expected = f"GET\n/agents\n2026-03-07T12:00:00+00:00\n{empty_hash}".encode()
        assert result == expected


# ---------------------------------------------------------------------------
# parse_authorization_header
# ---------------------------------------------------------------------------


class TestParseAuthorizationHeader:
    def test_valid_header(self) -> None:
        """Correctly extracts agent_id and signature bytes."""
        sig_bytes = b"test-signature-data"
        sig_b64 = base64.b64encode(sig_bytes).decode("utf-8")
        header = f"Signature agent-007:{sig_b64}"

        agent_id, signature = parse_authorization_header(header)

        assert agent_id == "agent-007"
        assert signature == sig_bytes

    def test_missing_prefix(self) -> None:
        """Raises ValueError when 'Signature ' prefix is missing."""
        with pytest.raises(ValueError, match="must start with 'Signature '"):
            parse_authorization_header("Bearer some-token")

    def test_missing_colon(self) -> None:
        """Raises ValueError when colon separator is missing."""
        with pytest.raises(ValueError, match="must contain 'agent_id:signature'"):
            parse_authorization_header("Signature no-colon-here")

    def test_empty_agent_id(self) -> None:
        """Raises ValueError when agent_id portion is empty."""
        sig_b64 = base64.b64encode(b"sig").decode("utf-8")
        with pytest.raises(ValueError, match="Empty agent_id or signature"):
            parse_authorization_header(f"Signature :{sig_b64}")

    def test_empty_signature(self) -> None:
        """Raises ValueError when signature portion is empty."""
        with pytest.raises(ValueError, match="Empty agent_id or signature"):
            parse_authorization_header("Signature agent-007:")


# ---------------------------------------------------------------------------
# validate_timestamp
# ---------------------------------------------------------------------------


class TestValidateTimestamp:
    def test_valid_timestamp(self) -> None:
        """Current time is within window."""
        now = datetime.now(UTC)
        result = validate_timestamp(now.isoformat())
        assert isinstance(result, datetime)
        assert result.tzinfo is not None

    def test_expired_past(self) -> None:
        """Timestamp 10 minutes in the past raises ValueError."""
        old = datetime.now(UTC) - timedelta(minutes=10)
        with pytest.raises(ValueError, match="outside the .* replay window"):
            validate_timestamp(old.isoformat())

    def test_expired_future(self) -> None:
        """Timestamp 10 minutes in the future raises ValueError."""
        future = datetime.now(UTC) + timedelta(minutes=10)
        with pytest.raises(ValueError, match="outside the .* replay window"):
            validate_timestamp(future.isoformat())

    def test_malformed_timestamp(self) -> None:
        """Non-ISO-format string raises ValueError."""
        with pytest.raises(ValueError, match="Invalid timestamp format"):
            validate_timestamp("not-a-date")

    def test_naive_timestamp_treated_as_utc(self) -> None:
        """Timestamp without timezone info is treated as UTC."""
        now_naive = datetime.now(UTC).replace(tzinfo=None).isoformat()
        result = validate_timestamp(now_naive)
        assert result.tzinfo == UTC


# ---------------------------------------------------------------------------
# verify_signature
# ---------------------------------------------------------------------------


class TestVerifySignature:
    def test_valid_signature(self) -> None:
        """Sign-and-verify round-trip succeeds."""
        private_key = Ed25519PrivateKey.generate()
        public_key = private_key.public_key()
        message = b"test-signing-string"

        signature = private_key.sign(message)
        assert verify_signature(public_key, message, signature) is True

    def test_invalid_signature_wrong_key(self) -> None:
        """Verification with a different key returns False."""
        private_key_a = Ed25519PrivateKey.generate()
        private_key_b = Ed25519PrivateKey.generate()
        message = b"test-signing-string"

        signature = private_key_a.sign(message)
        assert verify_signature(private_key_b.public_key(), message, signature) is False

    def test_tampered_message(self) -> None:
        """Verification with altered signing string returns False."""
        private_key = Ed25519PrivateKey.generate()
        public_key = private_key.public_key()
        message = b"original-message"

        signature = private_key.sign(message)
        assert verify_signature(public_key, b"tampered-message", signature) is False


# ---------------------------------------------------------------------------
# sign_request helper
# ---------------------------------------------------------------------------


class TestSignRequestHelper:
    def test_generates_valid_headers(self) -> None:
        """sign_request() produces headers that pass verification."""
        private_key, _ = generate_test_keypair()
        public_key = private_key.public_key()
        body = b'{"action": "test"}'
        method = "POST"
        path = "/tasks"

        headers = sign_request(method, path, body, private_key, "test-agent")

        # Extract parts
        assert "Authorization" in headers
        assert "X-Fleet-Timestamp" in headers
        assert headers["Authorization"].startswith("Signature test-agent:")

        # Parse and verify
        agent_id, signature = parse_authorization_header(headers["Authorization"])
        assert agent_id == "test-agent"

        signing_string = build_signing_string(
            method, path, headers["X-Fleet-Timestamp"], body
        )
        assert verify_signature(public_key, signing_string, signature) is True

    def test_custom_timestamp(self) -> None:
        """sign_request() respects a caller-provided timestamp."""
        private_key, _ = generate_test_keypair()
        ts = datetime(2026, 3, 7, 12, 0, 0, tzinfo=UTC)

        headers = sign_request(
            "GET", "/agents", None, private_key, "agent-1", timestamp=ts
        )
        assert headers["X-Fleet-Timestamp"] == ts.isoformat()

    def test_none_body_treated_as_empty(self) -> None:
        """sign_request(body=None) signs over empty bytes."""
        private_key, _ = generate_test_keypair()
        public_key = private_key.public_key()

        headers = sign_request("GET", "/agents", None, private_key, "agent-1")
        _, signature = parse_authorization_header(headers["Authorization"])

        signing_string = build_signing_string(
            "GET", "/agents", headers["X-Fleet-Timestamp"], b""
        )
        assert verify_signature(public_key, signing_string, signature) is True
