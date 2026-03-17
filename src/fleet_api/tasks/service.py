"""Task business logic — re-export layer for backwards compatibility.

This module previously contained all task service logic (~1,854 lines).
It has been split into focused modules:
  - responses.py: Response builders, HATEOAS links, cursor pagination
  - lifecycle.py: State transition operations (cancel, retask, redirect, pause, resume)
  - context.py: Context injection operations
  - crud.py: TaskService class (principal-side CRUD)
  - sidecar.py: Executor-side event processing

All public names are re-exported here so existing imports continue to work:
    from fleet_api.tasks.service import TaskService  # still works
"""

from __future__ import annotations

# Re-export schedule_callback so tests that patch
# "fleet_api.tasks.service.schedule_callback" continue to work.
from fleet_api.tasks.callbacks import schedule_callback  # noqa: F401

# Re-export: context injection operations
from fleet_api.tasks.context import (  # noqa: F401
    count_context_injections,
    get_max_context_sequence,
    inject_context,
)

# Re-export: CRUD service
from fleet_api.tasks.crud import TaskService, check_idempotency  # noqa: F401

# Re-export: lifecycle operations
from fleet_api.tasks.lifecycle import (  # noqa: F401
    build_lineage_chain,
    cancel_task,
    pause_task,
    redirect_task,
    resume_task,
    retask_task,
)

# Re-export: response builders and constants
from fleet_api.tasks.responses import (  # noqa: F401
    _ACTION_LINK_SUFFIX,
    _STATUS_ACTION_LINKS,
    IDEMPOTENCY_TTL_HOURS,
    build_task_links,
    decode_task_cursor,
    encode_task_cursor,
    task_to_detail_response,
    task_to_summary_response,
)

# Re-export: sidecar event processing (executor-side)
from fleet_api.tasks.sidecar import process_sidecar_event  # noqa: F401

__all__ = [
    # Constants
    "IDEMPOTENCY_TTL_HOURS",
    "_STATUS_ACTION_LINKS",
    "_ACTION_LINK_SUFFIX",
    # Response builders
    "build_task_links",
    "task_to_detail_response",
    "task_to_summary_response",
    "encode_task_cursor",
    "decode_task_cursor",
    # Lifecycle
    "cancel_task",
    "retask_task",
    "build_lineage_chain",
    "redirect_task",
    "pause_task",
    "resume_task",
    # Context
    "count_context_injections",
    "get_max_context_sequence",
    "inject_context",
    # CRUD
    "TaskService",
    "check_idempotency",
    "process_sidecar_event",
    # Callback (for test patching compatibility)
    "schedule_callback",
]
