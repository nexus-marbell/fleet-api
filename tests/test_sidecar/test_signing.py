"""Tests for fleet_agent.signing -- must produce headers that fleet-api accepts."""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from fleet_agent.signing import sign_request


class TestSignRequest:
    """Ed25519 request signing matches fleet-api's verification format."""

    def test_produces_authorization_and_timestamp_headers(self) -> None:
        """sign_request returns both required headers."""
        private_key = Ed25519PrivateKey.generate()
        headers = sign_request(
            method="GET",
            path="/agents/a1/tasks/pending",
            body=b"",
            private_key=private_key,
            agent_id="a1",
        )
        assert "Authorization" in headers
        assert "X-Fleet-Timestamp" in headers

    def test_authorization_format(self) -> None:
        """Authorization header follows 'Signature agent_id:base64sig' format."""
        private_key = Ed25519PrivateKey.generate()
        headers = sign_request(
            method="POST",
            path="/tasks/t1/events",
            body=b'{"event_type":"status"}',
            private_key=private_key,
            agent_id="nexus",
        )
        auth = headers["Authorization"]
        assert auth.startswith("Signature nexus:")
        # The remainder should be valid base64.
        sig_b64 = auth.split(":", 1)[1]
        decoded = base64.b64decode(sig_b64)
        # Ed25519 signatures are 64 bytes.
        assert len(decoded) == 64

    def test_signature_verifies_against_public_key(self) -> None:
        """Signature can be verified with the corresponding public key."""
        private_key = Ed25519PrivateKey.generate()
        public_key = private_key.public_key()
        body = b'{"test": true}'

        headers = sign_request(
            method="POST",
            path="/tasks/t1/events",
            body=body,
            private_key=private_key,
            agent_id="agent-x",
        )

        # Rebuild signing string the same way fleet-api does.
        timestamp = headers["X-Fleet-Timestamp"]
        body_hash = hashlib.sha256(body).hexdigest()
        signing_string = f"POST\n/tasks/t1/events\n{timestamp}\n{body_hash}".encode()

        sig_b64 = headers["Authorization"].split(":", 1)[1]
        signature = base64.b64decode(sig_b64)

        # Should not raise.
        public_key.verify(signature, signing_string)

    def test_query_string_included_in_signature_when_present(self) -> None:
        """Signing a path with a query string produces a different signature.

        This verifies the documented contract: callers must strip query
        parameters before signing.  If they don't, the signature covers the
        query string and will not match the server-side verification (which
        signs the bare path only).
        """
        private_key = Ed25519PrivateKey.generate()
        public_key = private_key.public_key()

        bare_path = "/agents/a1/tasks/pending"
        path_with_qs = "/agents/a1/tasks/pending?limit=10"
        body = b""

        headers_bare = sign_request(
            method="GET",
            path=bare_path,
            body=body,
            private_key=private_key,
            agent_id="a1",
        )
        headers_qs = sign_request(
            method="GET",
            path=path_with_qs,
            body=body,
            private_key=private_key,
            agent_id="a1",
        )

        # Signatures must differ because the path component differs.
        sig_bare = headers_bare["Authorization"].split(":", 1)[1]
        sig_qs = headers_qs["Authorization"].split(":", 1)[1]
        assert sig_bare != sig_qs, (
            "Signing a path with a query string should produce a different "
            "signature than the bare path"
        )

        # The bare-path signature verifies against the bare signing string.
        ts_bare = headers_bare["X-Fleet-Timestamp"]
        body_hash = hashlib.sha256(body).hexdigest()
        signing_string = f"GET\n{bare_path}\n{ts_bare}\n{body_hash}".encode()
        signature = base64.b64decode(sig_bare)
        public_key.verify(signature, signing_string)

        # The query-string signature does NOT verify against the bare path --
        # demonstrating why callers must strip query params.
        ts_qs = headers_qs["X-Fleet-Timestamp"]
        bare_signing_string = f"GET\n{bare_path}\n{ts_qs}\n{body_hash}".encode()
        qs_signature = base64.b64decode(sig_qs)
        with pytest.raises(Exception):
            public_key.verify(qs_signature, bare_signing_string)

    def test_empty_body_uses_sha256_of_empty(self) -> None:
        """GET requests with empty body sign against sha256('')."""
        private_key = Ed25519PrivateKey.generate()
        public_key = private_key.public_key()

        with patch("fleet_agent.signing.datetime") as mock_dt:
            fixed_ts = datetime(2026, 3, 7, 12, 0, 0, tzinfo=UTC)
            mock_dt.now.return_value = fixed_ts
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            headers = sign_request(
                method="GET",
                path="/agents/a1/tasks/pending",
                body=b"",
                private_key=private_key,
                agent_id="a1",
            )

        timestamp = headers["X-Fleet-Timestamp"]
        empty_hash = hashlib.sha256(b"").hexdigest()
        signing_string = f"GET\n/agents/a1/tasks/pending\n{timestamp}\n{empty_hash}".encode()

        sig_b64 = headers["Authorization"].split(":", 1)[1]
        signature = base64.b64decode(sig_b64)
        public_key.verify(signature, signing_string)
