"""RFC field compliance tests — prevents field name drift from the specification.

These tests validate that API response builders use only field names defined
in rfc_field_mapping.yaml. This catches the recurring "RFC field drift" pattern
where code uses different names than the spec (e.g., retask_depth vs lineage_depth).

The mapping file is the single source of truth. When adding new fields:
1. Add to rfc_field_mapping.yaml first
2. Then implement in code
3. These tests enforce that order

Reference: nexus-marbell/fleet-api Phase 2 review pattern (Nexus caught
field drift on every PR). Issue #49 cleanup item.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from fleet_api.tasks.models import Task, TaskEvent, TaskPriority, TaskStatus
from fleet_api.tasks.service import (
    build_task_links,
    task_to_detail_response,
    task_to_summary_response,
)


@pytest.fixture(scope="module")
def rfc_fields() -> dict[str, list[str]]:
    """Load canonical field names from rfc_field_mapping.yaml."""
    mapping_path = Path(__file__).parent.parent / "rfc_field_mapping.yaml"
    with open(mapping_path) as f:
        return yaml.safe_load(f)


def _make_stub_task(**overrides: object) -> Task:
    """Create a minimal Task object for testing response builders."""
    from datetime import UTC, datetime

    defaults: dict[str, object] = {
        "id": "test-task-id",
        "workflow_id": "test-workflow-id",
        "principal_agent_id": "agent-001",
        "executor_agent_id": "agent-002",
        "status": TaskStatus.RUNNING,
        "input": {"prompt": "test"},
        "priority": TaskPriority.NORMAL,
    }
    defaults.update(overrides)
    task = Task(**defaults)
    # Set optional fields that may not be in the constructor
    if not hasattr(task, "started_at") or task.started_at is None:
        task.started_at = datetime.now(UTC)
    if not hasattr(task, "completed_at"):
        task.completed_at = None
    if not hasattr(task, "paused_at"):
        task.paused_at = None
    return task


class TestTaskDetailResponse:
    """Validate task_to_detail_response uses only RFC-canonical field names."""

    def test_all_fields_are_canonical(self, rfc_fields: dict[str, list[str]]) -> None:
        """Every top-level key in detail response must be in the mapping."""
        task = _make_stub_task()
        response = task_to_detail_response(task)
        canonical = set(rfc_fields["task_detail"])
        actual = set(response.keys())
        unexpected = actual - canonical
        assert not unexpected, (
            f"task_to_detail_response contains fields not in rfc_field_mapping.yaml: "
            f"{unexpected}. Add them to 'task_detail' in the mapping if they are "
            f"RFC-compliant, or rename them to match the RFC."
        )

    def test_completed_task_fields_are_canonical(
        self, rfc_fields: dict[str, list[str]]
    ) -> None:
        """Completed task with result should also use canonical names."""
        from datetime import UTC, datetime

        task = _make_stub_task(
            status=TaskStatus.COMPLETED,
            result={"output": "done"},
            completed_at=datetime.now(UTC),
        )
        response = task_to_detail_response(task)
        canonical = set(rfc_fields["task_detail"])
        actual = set(response.keys())
        unexpected = actual - canonical
        assert not unexpected, (
            f"Completed task detail response has non-canonical fields: {unexpected}"
        )


class TestTaskSummaryResponse:
    """Validate task_to_summary_response uses only RFC-canonical field names."""

    def test_all_fields_are_canonical(self, rfc_fields: dict[str, list[str]]) -> None:
        task = _make_stub_task()
        response = task_to_summary_response(task)
        canonical = set(rfc_fields["task_summary"])
        actual = set(response.keys())
        unexpected = actual - canonical
        assert not unexpected, (
            f"task_to_summary_response contains non-canonical fields: {unexpected}. "
            f"Add them to 'task_summary' in rfc_field_mapping.yaml or rename."
        )


class TestHATEOASLinks:
    """Validate link relation names match RFC."""

    @pytest.mark.parametrize(
        "status",
        [s for s in TaskStatus],
        ids=[s.name for s in TaskStatus],
    )
    def test_link_relations_are_canonical(
        self, status: TaskStatus, rfc_fields: dict[str, list[str]]
    ) -> None:
        """Every link relation in every status must be in the mapping."""
        links = build_task_links("task-1", "wf-1", status)
        canonical = set(rfc_fields["hateoas_links"])
        actual = set(links.keys())
        unexpected = actual - canonical
        assert not unexpected, (
            f"build_task_links({status.name}) has non-canonical link relations: "
            f"{unexpected}. Add to 'hateoas_links' in rfc_field_mapping.yaml."
        )
