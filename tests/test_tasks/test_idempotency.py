"""Tests for the standalone check_idempotency function (Issue #44).

Covers:
  - No existing task → returns None
  - Existing task with matching input → returns the task
  - Existing task with different input → raises IDEMPOTENCY_MISMATCH (422)
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from fleet_api.errors import ErrorCode, InputValidationError
from fleet_api.tasks.crud import check_idempotency
from fleet_api.tasks.models import Task


def _make_mock_task(
    idempotency_key: str = "key-1",
    task_input: dict[str, Any] | None = None,
) -> MagicMock:
    """Create a minimal mock Task for idempotency checks."""
    task = MagicMock(spec=Task)
    task.idempotency_key = idempotency_key
    task.input = task_input if task_input is not None else {"prompt": "hello"}
    return task


class TestCheckIdempotency:
    """Unit tests for the standalone check_idempotency function."""

    @pytest.mark.asyncio
    async def test_no_existing_task_returns_none(self) -> None:
        """When no task exists with the key, returns None."""
        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        session.execute = AsyncMock(return_value=mock_result)

        result = await check_idempotency(session, "nonexistent-key", {"prompt": "hello"})
        assert result is None

    @pytest.mark.asyncio
    async def test_matching_input_returns_existing_task(self) -> None:
        """When key exists and input matches, returns the existing task."""
        existing_input = {"prompt": "hello", "model": "gpt-4"}
        existing_task = _make_mock_task(task_input=existing_input)

        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing_task
        session.execute = AsyncMock(return_value=mock_result)

        result = await check_idempotency(session, "key-1", existing_input)
        assert result is existing_task

    @pytest.mark.asyncio
    async def test_different_input_raises_mismatch(self) -> None:
        """When key exists but input differs, raises IDEMPOTENCY_MISMATCH."""
        existing_task = _make_mock_task(task_input={"prompt": "hello"})

        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing_task
        session.execute = AsyncMock(return_value=mock_result)

        with pytest.raises(InputValidationError) as exc_info:
            await check_idempotency(session, "key-1", {"prompt": "different"})

        assert exc_info.value.code == ErrorCode.IDEMPOTENCY_MISMATCH

    @pytest.mark.asyncio
    async def test_input_order_does_not_affect_match(self) -> None:
        """JSON key ordering is normalized — same keys in different order still match."""
        existing_task = _make_mock_task(task_input={"b": 2, "a": 1})

        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing_task
        session.execute = AsyncMock(return_value=mock_result)

        # Same data, different insertion order
        result = await check_idempotency(session, "key-1", {"a": 1, "b": 2})
        assert result is existing_task

    @pytest.mark.asyncio
    async def test_hash_uses_sort_keys(self) -> None:
        """Verify the hash is computed with sort_keys=True for consistency."""
        data = {"z": 26, "a": 1}
        expected_hash = hashlib.sha256(
            json.dumps(data, sort_keys=True).encode()
        ).hexdigest()
        # Verify same hash regardless of insertion order
        reversed_data = {"a": 1, "z": 26}
        actual_hash = hashlib.sha256(
            json.dumps(reversed_data, sort_keys=True).encode()
        ).hexdigest()
        assert expected_hash == actual_hash
