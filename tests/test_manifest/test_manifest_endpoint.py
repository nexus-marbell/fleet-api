"""Tests for GET /manifest endpoint."""

import pytest


class TestManifestResponse:
    """Verify the manifest response structure and content."""

    @pytest.mark.asyncio
    async def test_returns_200(self, client):
        """GET /manifest returns 200 OK."""
        response = await client.get("/manifest")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_unauthenticated_request_succeeds(self, client):
        """Manifest is accessible without authentication headers."""
        # No Authorization or X-Fleet-Timestamp headers
        response = await client.get("/manifest")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_response_is_json(self, client):
        """Response content type is application/json."""
        response = await client.get("/manifest")
        assert response.headers["content-type"] == "application/json"

    @pytest.mark.asyncio
    async def test_top_level_fields_present(self, client):
        """All required top-level fields are present."""
        response = await client.get("/manifest")
        data = response.json()
        required_fields = {
            "name",
            "version",
            "description",
            "base_url",
            "auth",
            "capabilities",
            "rate_limits",
            "parameter_conventions",
            "schema_changelog",
            "_links",
        }
        assert required_fields.issubset(data.keys()), (
            f"Missing fields: {required_fields - data.keys()}"
        )

    @pytest.mark.asyncio
    async def test_name_and_version(self, client):
        """Name is Fleet API, version is a semver string."""
        data = (await client.get("/manifest")).json()
        assert data["name"] == "Fleet API"
        parts = data["version"].split(".")
        assert len(parts) == 3
        assert all(p.isdigit() for p in parts)

    @pytest.mark.asyncio
    async def test_description_is_string(self, client):
        """Description is a non-empty string."""
        data = (await client.get("/manifest")).json()
        assert isinstance(data["description"], str)
        assert len(data["description"]) > 0


class TestManifestAuth:
    """Verify the auth section of the manifest."""

    @pytest.mark.asyncio
    async def test_auth_type(self, client):
        """Auth type is ed25519-signature."""
        data = (await client.get("/manifest")).json()
        assert data["auth"]["type"] == "ed25519-signature"

    @pytest.mark.asyncio
    async def test_auth_header(self, client):
        """Auth header is Authorization."""
        data = (await client.get("/manifest")).json()
        assert data["auth"]["header"] == "Authorization"

    @pytest.mark.asyncio
    async def test_auth_format(self, client):
        """Auth format describes the Signature scheme."""
        data = (await client.get("/manifest")).json()
        assert "Signature" in data["auth"]["format"]
        assert "<agent_id>" in data["auth"]["format"]

    @pytest.mark.asyncio
    async def test_auth_key_registration(self, client):
        """Key registration path points to /agents/register."""
        data = (await client.get("/manifest")).json()
        assert data["auth"]["key_registration"] == "/agents/register"

    @pytest.mark.asyncio
    async def test_server_public_key_present(self, client):
        """Server public key is a PEM-encoded Ed25519 key (generated or loaded)."""
        data = (await client.get("/manifest")).json()
        key = data["auth"]["server_public_key"]
        assert key is not None
        assert key.startswith("-----BEGIN PUBLIC KEY-----")
        assert key.strip().endswith("-----END PUBLIC KEY-----")

    @pytest.mark.asyncio
    async def test_auth_fields_match_rfc(self, client):
        """Auth section contains exactly the RFC-specified fields."""
        data = (await client.get("/manifest")).json()
        expected_keys = {"type", "header", "format", "key_registration", "server_public_key"}
        assert set(data["auth"].keys()) == expected_keys


class TestManifestCapabilities:
    """Verify capabilities list reflects Phase 1 + Phase 2."""

    @pytest.mark.asyncio
    async def test_capabilities_is_list(self, client):
        """Capabilities is a list."""
        data = (await client.get("/manifest")).json()
        assert isinstance(data["capabilities"], list)

    @pytest.mark.asyncio
    async def test_phase1_capabilities_present(self, client):
        """Phase 1 capabilities are listed."""
        data = (await client.get("/manifest")).json()
        caps = data["capabilities"]
        assert "workflow_registry" in caps
        assert "task_dispatch" in caps

    @pytest.mark.asyncio
    async def test_phase2_capabilities_present(self, client):
        """Phase 2 capabilities are listed."""
        data = (await client.get("/manifest")).json()
        caps = data["capabilities"]
        phase2 = {
            "sse_streaming",
            "pause_resume",
            "context_injection",
            "retask_with_lineage",
            "redirect",
            "callback_signing",
            "idempotent_writes",
            "pull_dispatch",
            "agent_heartbeat",
        }
        missing = phase2 - set(caps)
        assert not missing, f"Missing Phase 2 capabilities: {missing}"

    @pytest.mark.asyncio
    async def test_no_aspirational_capabilities(self, client):
        """Aspirational features not yet implemented are NOT listed."""
        data = (await client.get("/manifest")).json()
        caps = data["capabilities"]
        aspirational = {"webhooks", "batch_operations", "plugins"}
        present_aspirational = aspirational & set(caps)
        assert not present_aspirational, (
            f"Aspirational capabilities should not be listed: {present_aspirational}"
        )


class TestManifestRateLimits:
    """Verify rate limits section is honest about enforcement status."""

    @pytest.mark.asyncio
    async def test_rate_limits_marked_as_planned(self, client):
        """Rate limits status is 'planned' (no enforcement middleware)."""
        data = (await client.get("/manifest")).json()
        rl = data["rate_limits"]
        assert rl["status"] == "planned"
        assert "description" in rl


class TestManifestParameterConventions:
    """Verify parameter conventions match RFC section 3.1."""

    @pytest.mark.asyncio
    async def test_canonical_parameters_present(self, client):
        """Canonical parameters limit, cursor, status are defined."""
        data = (await client.get("/manifest")).json()
        pc = data["parameter_conventions"]
        assert "limit" in pc
        assert "cursor" in pc
        assert "status" in pc

    @pytest.mark.asyncio
    async def test_limit_convention_shape(self, client):
        """limit convention has description, type, default, max, not."""
        data = (await client.get("/manifest")).json()
        limit = data["parameter_conventions"]["limit"]
        assert limit["description"] == "Maximum number of results to return"
        assert limit["type"] == "integer"
        assert limit["default"] == 20
        assert limit["max"] == 100
        assert isinstance(limit["not"], list)
        assert "count" in limit["not"]

    @pytest.mark.asyncio
    async def test_cursor_convention_shape(self, client):
        """cursor convention has description, type, not."""
        data = (await client.get("/manifest")).json()
        cursor = data["parameter_conventions"]["cursor"]
        assert cursor["type"] == "string"
        assert "page" in cursor["not"]

    @pytest.mark.asyncio
    async def test_status_convention_shape(self, client):
        """status convention has description, type, not."""
        data = (await client.get("/manifest")).json()
        status = data["parameter_conventions"]["status"]
        assert status["type"] == "string"
        assert "state" in status["not"]

    @pytest.mark.asyncio
    async def test_not_lists_are_rejected_aliases(self, client):
        """Each convention's 'not' list contains rejected alias names."""
        data = (await client.get("/manifest")).json()
        pc = data["parameter_conventions"]
        for name, conv in pc.items():
            assert "not" in conv, f"Convention '{name}' missing 'not' list"
            assert isinstance(conv["not"], list)
            assert len(conv["not"]) > 0, f"Convention '{name}' has empty 'not' list"


class TestManifestSchemaChangelog:
    """Verify schema changelog structure."""

    @pytest.mark.asyncio
    async def test_changelog_is_list(self, client):
        """Schema changelog is a non-empty list."""
        data = (await client.get("/manifest")).json()
        assert isinstance(data["schema_changelog"], list)
        assert len(data["schema_changelog"]) > 0

    @pytest.mark.asyncio
    async def test_changelog_entry_structure(self, client):
        """Each changelog entry has version, date, changes, breaking."""
        data = (await client.get("/manifest")).json()
        entry = data["schema_changelog"][0]
        assert "version" in entry
        assert "date" in entry
        assert "changes" in entry
        assert isinstance(entry["changes"], list)
        assert "breaking" in entry
        assert isinstance(entry["breaking"], bool)

    @pytest.mark.asyncio
    async def test_initial_release_not_breaking(self, client):
        """Initial release changelog entry is not breaking."""
        data = (await client.get("/manifest")).json()
        entry = data["schema_changelog"][0]
        assert entry["breaking"] is False


class TestManifestLinks:
    """Verify HATEOAS _links in manifest."""

    @pytest.mark.asyncio
    async def test_links_present(self, client):
        """_links object is present."""
        data = (await client.get("/manifest")).json()
        assert "_links" in data
        assert isinstance(data["_links"], dict)

    @pytest.mark.asyncio
    async def test_self_link(self, client):
        """Self link points to /manifest."""
        data = (await client.get("/manifest")).json()
        assert data["_links"]["self"]["href"].endswith("/manifest")

    @pytest.mark.asyncio
    async def test_required_links(self, client):
        """All top-level endpoint links are present (only real endpoints)."""
        data = (await client.get("/manifest")).json()
        links = data["_links"]
        required = {"self", "agents", "workflows", "tasks", "health", "openapi"}
        assert required.issubset(links.keys()), f"Missing links: {required - links.keys()}"

    @pytest.mark.asyncio
    async def test_no_phantom_links(self, client):
        """Phantom links to nonexistent endpoints are NOT present."""
        data = (await client.get("/manifest")).json()
        links = data["_links"]
        phantom = {"tools", "errors", "status"}
        present_phantom = phantom & set(links.keys())
        assert not present_phantom, (
            f"Phantom links to nonexistent endpoints: {present_phantom}"
        )

    @pytest.mark.asyncio
    async def test_links_have_href(self, client):
        """Every link object has an href field."""
        data = (await client.get("/manifest")).json()
        for name, link in data["_links"].items():
            assert "href" in link, f"Link '{name}' missing href"
            assert isinstance(link["href"], str)

    @pytest.mark.asyncio
    async def test_register_link_has_method(self, client):
        """The agents_register link specifies POST method."""
        data = (await client.get("/manifest")).json()
        assert data["_links"]["agents_register"]["method"] == "POST"


class TestManifestHeaders:
    """Verify custom response headers."""

    @pytest.mark.asyncio
    async def test_schema_version_header(self, client):
        """X-Schema-Version header is present."""
        response = await client.get("/manifest")
        assert "x-schema-version" in response.headers
        # Value should be a semver string
        parts = response.headers["x-schema-version"].split(".")
        assert len(parts) == 3

    @pytest.mark.asyncio
    async def test_no_rate_limit_headers(self, client):
        """Rate limit headers are NOT sent (no enforcement middleware)."""
        response = await client.get("/manifest")
        assert "x-ratelimit-limit" not in response.headers
        assert "x-ratelimit-remaining" not in response.headers
        assert "x-ratelimit-reset" not in response.headers

    @pytest.mark.asyncio
    async def test_cache_control_header(self, client):
        """Cache-Control header is set for public caching."""
        response = await client.get("/manifest")
        assert "cache-control" in response.headers
        assert "public" in response.headers["cache-control"]
