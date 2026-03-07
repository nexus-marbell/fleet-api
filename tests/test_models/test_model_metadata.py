"""Tests that verify SQLAlchemy model metadata without requiring a database.

These tests inspect the model's column definitions, types, and constraints
by examining the SQLAlchemy metadata directly.
"""

from sqlalchemy import inspect as sa_inspect

from fleet_api.agents.models import Agent, AgentStatus
from fleet_api.tasks.models import Task, TaskEvent, TaskPriority, TaskStatus
from fleet_api.workflows.models import Workflow, WorkflowStatus


class TestAgentModel:
    """Agent model column and constraint verification."""

    def test_tablename(self) -> None:
        assert Agent.__tablename__ == "agents"

    def test_primary_key(self) -> None:
        mapper = sa_inspect(Agent)
        pk_cols = [col.name for col in mapper.primary_key]
        assert pk_cols == ["id"]

    def test_required_columns(self) -> None:
        mapper = sa_inspect(Agent)
        col_names = {col.key for col in mapper.columns}
        expected = {
            "id",
            "display_name",
            "public_key",
            "capabilities",
            "status",
            "last_heartbeat",
            "registered_at",
            "metadata",
        }
        assert expected.issubset(col_names)

    def test_public_key_not_nullable(self) -> None:
        mapper = sa_inspect(Agent)
        col = mapper.columns["public_key"]
        assert not col.nullable

    def test_status_has_server_default(self) -> None:
        mapper = sa_inspect(Agent)
        col = mapper.columns["status"]
        assert col.server_default is not None

    def test_agent_status_values(self) -> None:
        values = {s.value for s in AgentStatus}
        assert values == {"registered", "active", "unreachable", "suspended"}


class TestWorkflowModel:
    """Workflow model column and constraint verification."""

    def test_tablename(self) -> None:
        assert Workflow.__tablename__ == "workflows"

    def test_primary_key(self) -> None:
        mapper = sa_inspect(Workflow)
        pk_cols = [col.name for col in mapper.primary_key]
        assert pk_cols == ["id"]

    def test_owner_agent_id_is_fk(self) -> None:
        mapper = sa_inspect(Workflow)
        col = mapper.columns["owner_agent_id"]
        assert len(col.foreign_keys) == 1
        fk = next(iter(col.foreign_keys))
        assert fk.target_fullname == "agents.id"

    def test_owner_agent_id_not_nullable(self) -> None:
        mapper = sa_inspect(Workflow)
        col = mapper.columns["owner_agent_id"]
        assert not col.nullable

    def test_result_retention_days_default(self) -> None:
        mapper = sa_inspect(Workflow)
        col = mapper.columns["result_retention_days"]
        assert col.server_default is not None

    def test_workflow_status_values(self) -> None:
        values = {s.value for s in WorkflowStatus}
        assert values == {"active", "deprecated"}


class TestTaskModel:
    """Task model column and constraint verification."""

    def test_tablename(self) -> None:
        assert Task.__tablename__ == "tasks"

    def test_primary_key(self) -> None:
        mapper = sa_inspect(Task)
        pk_cols = [col.name for col in mapper.primary_key]
        assert pk_cols == ["id"]

    def test_workflow_id_fk(self) -> None:
        mapper = sa_inspect(Task)
        col = mapper.columns["workflow_id"]
        assert len(col.foreign_keys) == 1
        fk = next(iter(col.foreign_keys))
        assert fk.target_fullname == "workflows.id"

    def test_principal_agent_id_fk(self) -> None:
        mapper = sa_inspect(Task)
        col = mapper.columns["principal_agent_id"]
        fk = next(iter(col.foreign_keys))
        assert fk.target_fullname == "agents.id"

    def test_executor_agent_id_nullable(self) -> None:
        mapper = sa_inspect(Task)
        col = mapper.columns["executor_agent_id"]
        assert col.nullable

    def test_self_referential_fks(self) -> None:
        mapper = sa_inspect(Task)
        for col_name in ("parent_task_id", "root_task_id"):
            col = mapper.columns[col_name]
            assert col.nullable
            fk = next(iter(col.foreign_keys))
            assert fk.target_fullname == "tasks.id"

    def test_idempotency_key_unique(self) -> None:
        mapper = sa_inspect(Task)
        col = mapper.columns["idempotency_key"]
        assert col.unique

    def test_input_not_nullable(self) -> None:
        mapper = sa_inspect(Task)
        col = mapper.columns["input"]
        assert not col.nullable

    def test_task_status_values(self) -> None:
        values = {s.value for s in TaskStatus}
        assert values == {
            "accepted",
            "running",
            "paused",
            "completed",
            "failed",
            "cancelled",
            "retasked",
            "redirected",
        }

    def test_task_priority_values(self) -> None:
        values = {s.value for s in TaskPriority}
        assert values == {"low", "normal", "high", "critical"}

    def test_depth_columns_have_defaults(self) -> None:
        mapper = sa_inspect(Task)
        for col_name in ("retask_depth", "delegation_depth"):
            col = mapper.columns[col_name]
            assert col.server_default is not None


class TestTaskEventModel:
    """TaskEvent model column and constraint verification."""

    def test_tablename(self) -> None:
        assert TaskEvent.__tablename__ == "task_events"

    def test_primary_key_auto(self) -> None:
        mapper = sa_inspect(TaskEvent)
        pk_cols = [col.name for col in mapper.primary_key]
        assert pk_cols == ["id"]

    def test_task_id_fk(self) -> None:
        mapper = sa_inspect(TaskEvent)
        col = mapper.columns["task_id"]
        fk = next(iter(col.foreign_keys))
        assert fk.target_fullname == "tasks.id"

    def test_event_type_not_nullable(self) -> None:
        mapper = sa_inspect(TaskEvent)
        col = mapper.columns["event_type"]
        assert not col.nullable

    def test_sequence_not_nullable(self) -> None:
        mapper = sa_inspect(TaskEvent)
        col = mapper.columns["sequence"]
        assert not col.nullable
