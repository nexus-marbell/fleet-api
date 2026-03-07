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
            "rate_limit",
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
    async def test_auth_header_format(self, client):
        """Auth header format describes the Signature scheme."""
        data = (await client.get("/manifest")).json()
        assert "Signature" in data["auth"]["header_format"]
        assert "{agent_id}" in data["auth"]["header_format"]

    @pytest.mark.asyncio
    async def test_auth_key_registration_path(self, client):
        """Key registration path points to /agents/register."""
        data = (await client.get("/manifest")).json()
        assert data["auth"]["key_registration_path"] == "/agents/register"

    @pytest.mark.asyncio
    async def test_server_public_key_null_when_no_private_key(self, client):
        """Server public key is null when no private key is configured."""
        data = (await client.get("/manifest")).json()
        assert data["auth"]["server_public_key"] is None

    @pytest.mark.asyncio
    async def test_replay_window_seconds(self, client):
        """Replay window is documented in seconds."""
        data = (await client.get("/manifest")).json()
        assert data["auth"]["replay_window_seconds"] == 300

    @pytest.mark.asyncio
    async def test_timestamp_header(self, client):
        """Timestamp header name is documented."""
        data = (await client.get("/manifest")).json()
        assert data["auth"]["timestamp_header"] == "X-Fleet-Timestamp"


class TestManifestCapabilities:
    """Verify capabilities list reflects Phase 1 only."""

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
    async def test_no_aspirational_capabilities(self, client):
        """Aspirational features not yet implemented are NOT listed."""
        data = (await client.get("/manifest")).json()
        caps = data["capabilities"]
        aspirational = {"sse_streaming", "webhooks", "batch_operations", "plugins"}
        present_aspirational = aspirational & set(caps)
        assert not present_aspirational, (
            f"Aspirational capabilities should not be listed: {present_aspirational}"
        )


class TestManifestRateLimit:
    """Verify rate limit configuration in manifest."""

    @pytest.mark.asyncio
    async def test_rate_limit_structure(self, client):
        """Rate limit has requests_per_minute and burst."""
        data = (await client.get("/manifest")).json()
        rl = data["rate_limit"]
        assert "requests_per_minute" in rl
        assert "burst" in rl

    @pytest.mark.asyncio
    async def test_rate_limit_values_are_integers(self, client):
        """Rate limit values are positive integers."""
        data = (await client.get("/manifest")).json()
        rl = data["rate_limit"]
        assert isinstance(rl["requests_per_minute"], int)
        assert isinstance(rl["burst"], int)
        assert rl["requests_per_minute"] > 0
        assert rl["burst"] > 0


class TestManifestParameterConventions:
    """Verify parameter naming conventions and rejected aliases."""

    @pytest.mark.asyncio
    async def test_naming_convention(self, client):
        """Naming convention is snake_case."""
        data = (await client.get("/manifest")).json()
        assert data["parameter_conventions"]["naming"] == "snake_case"

    @pytest.mark.asyncio
    async def test_rejected_aliases_present(self, client):
        """Rejected aliases map camelCase to snake_case equivalents."""
        data = (await client.get("/manifest")).json()
        aliases = data["parameter_conventions"]["rejected_aliases"]
        assert isinstance(aliases, dict)
        assert len(aliases) > 0
        # Every key should be camelCase, every value snake_case
        for key, value in aliases.items():
            assert "_" not in key, f"Key '{key}' should be camelCase"
            assert "_" in value, f"Value '{value}' should be snake_case"


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
        """Each changelog entry has version, date, changes."""
        data = (await client.get("/manifest")).json()
        entry = data["schema_changelog"][0]
        assert "version" in entry
        assert "date" in entry
        assert "changes" in entry
        assert isinstance(entry["changes"], list)


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
        """All top-level endpoint links are present."""
        data = (await client.get("/manifest")).json()
        links = data["_links"]
        required = {"self", "agents", "workflows", "tasks", "health", "openapi"}
        assert required.issubset(links.keys()), f"Missing links: {required - links.keys()}"

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
    async def test_rate_limit_header(self, client):
        """X-RateLimit-Limit header is present."""
        response = await client.get("/manifest")
        assert "x-ratelimit-limit" in response.headers
        assert int(response.headers["x-ratelimit-limit"]) > 0

    @pytest.mark.asyncio
    async def test_rate_limit_remaining_header(self, client):
        """X-RateLimit-Remaining header is present."""
        response = await client.get("/manifest")
        assert "x-ratelimit-remaining" in response.headers
        assert int(response.headers["x-ratelimit-remaining"]) > 0

    @pytest.mark.asyncio
    async def test_rate_limit_reset_header(self, client):
        """X-RateLimit-Reset header is present and is a unix timestamp."""
        response = await client.get("/manifest")
        assert "x-ratelimit-reset" in response.headers
        reset = int(response.headers["x-ratelimit-reset"])
        # Should be a reasonable unix timestamp (after 2026-01-01)
        assert reset > 1_767_225_600

    @pytest.mark.asyncio
    async def test_cache_control_header(self, client):
        """Cache-Control header is set for public caching."""
        response = await client.get("/manifest")
        assert "cache-control" in response.headers
        assert "public" in response.headers["cache-control"]
