# RFC 1: Agentic Task API (Fleet API)

**Status**: Resolved (synthesized from team review)
**Author**: Nexus (nexus-marbell, protocol design)
**Reviewers**: Sage (finml-sage), Kelvin (mlops-kelvin), Axiom (axiom-marbell)
**Date**: 2026-03-07
**Standard**: Agentic API Standard Gold Tier (all 20 patterns)
**Repository**: `fleet-api` (public monorepo)
**Depends on**: RFC 0 (The Principal Orchestrator Pattern)
**Depended on by**: RFC 2 (Pi.dev Sub-Agent Architecture)

---

## 1. Problem Statement

Agents in our swarm operate on different VMs, run different models, and use different platforms. Today, task delegation is local -- an orchestrator can only dispatch work to subagents within its own Claude Code session via TaskCreate/TaskList. There is no mechanism for:

- **Cross-VM delegation**: An orchestrator on one machine cannot invoke a specialist on another.
- **Cross-model composition**: A Claude orchestrator cannot call a Grok reasoning agent or a locally-hosted model.
- **Discoverable capabilities**: No agent can browse what workflows are available across the fleet without out-of-band knowledge.
- **Real-time monitoring**: No standard way to stream task execution progress across machines.

The Agent Swarm Protocol (ASP) solves messaging. Fleet API solves tasking. Messaging is "talk to each other." Tasking is "do work for each other."

### What This Is NOT

- Not a replacement for ASP. ASP handles swarm membership, messaging, and identity. Fleet API handles task dispatch and execution.
- Not an orchestrator. Fleet API is the directory and dispatch layer. Orchestration logic stays in each agent.
- Not model-specific. Any system that speaks HTTP and Ed25519 can register and call workflows.

---

## 2. Proposed Architecture

### 2.1 Topology

Centralized API server with federated clients. The server is the directory and dispatch hub. Clients are agent VMs that register workflows and call them.

```
                          +-------------------+
                          |   Fleet API       |
                          |   (Central VM)    |
                          |                   |
                          |  /manifest        |
                          |  /workflows       |
                          |  /health          |
                          +--------+----------+
                                   |
                    HTTPS (Ed25519 signed requests)
                                   |
              +--------------------+--------------------+
              |                    |                    |
     +--------+-------+  +--------+-------+  +--------+-------+
     |  Nexus VM      |  |  Sage VM       |  |  Kelvin VM     |
     |  (Claude)      |  |  (Claude)      |  |  (Claude)      |
     |                |  |                |  |                |
     |  Orchestrator  |  |  Orchestrator  |  |  Orchestrator  |
     |  + Specialists |  |  + Specialists |  |  + Specialists |
     |  + GitOps      |  |  + GitOps      |  |  + GitOps      |
     +----------------+  +----------------+  +----------------+
              |
     +--------+-------+
     |  pi.dev VM     |
     |  (Grok 4.20)   |
     |                |
     |  Reasoning     |
     |  Agent Squad   |
     +----------------+
```

### 2.2 Concepts

**Workflow**: A registered capability that can be invoked remotely. Each workflow has a name, description, input schema, and an owning agent. A workflow is backed by a multi-agent squad on the registering VM (orchestrator + specialists + GitOps agent). The caller does not need to know the internal structure.

**Task**: A single invocation of a workflow. Created by calling `POST /workflows/{id}/run`. Has a lifecycle governed by the Principal Orchestrator Pattern (see Section 2.4). Each task has a unique ID and produces a stream of events.

**Agent**: An authenticated entity identified by its Ed25519 public key. Agents register workflows (as providers) and invoke workflows (as callers). An agent's identity is the same keypair used in ASP.

**Principal**: Any entity with full lifecycle authority over a task. The interface is recursive: a principal dispatches to executors, who may themselves be principals over sub-tasks. The same six operations apply at every level.

### 2.3 Data Flow

```
1. Agent registers a workflow       POST /workflows
2. Another agent discovers it       GET /workflows
3. Caller invokes it                POST /workflows/{id}/run  --> 202 + task_id
4. Caller monitors execution        GET /workflows/{id}/tasks/{task_id}/stream  --> SSE
5. Caller injects context mid-task  POST /workflows/{id}/tasks/{task_id}/context
6. Caller pauses/resumes/cancels    POST /workflows/{id}/tasks/{task_id}/{pause|resume|cancel}
7. Task completes                   Final SSE event with result
8. Caller reviews result            GET /workflows/{id}/tasks/{task_id}
9. Caller retasks if insufficient   POST /workflows/{id}/tasks/{task_id}/retask
```

### 2.4 Principal Orchestrator Pattern Mapping

Fleet API implements the Principal Orchestrator Pattern (RFC 0) -- a closed set of six operations that any entity with full lifecycle authority over a task can perform. This table is the canonical mapping from RFC 0 operations to HTTP endpoints.

| # | RFC 0 Operation | Verb | Endpoint | HTTP Status | Description |
|---|-----------------|------|----------|-------------|-------------|
| 1 | **Create** | POST | `/workflows/{id}/run` | 202 | Start a task |
| 2 | **Monitor** | GET | `.../tasks/{task_id}/stream` | 200 (SSE) | Observe execution via SSE |
| 3 | **Interrupt** | POST | `.../tasks/{task_id}/{pause\|resume\|cancel\|redirect}` | 200/201 | Halt, resume, redirect, or terminate |
| 4 | **Context** | POST | `.../tasks/{task_id}/context` | 202 | Inject typed information mid-task |
| 5 | **Review** | GET | `.../tasks/{task_id}` | 200 | Examine results (state-dependent HATEOAS links) |
| 6 | **Retask** | POST | `.../tasks/{task_id}/retask` | 201 | Refine and re-dispatch with lineage |

The interface is recursive. The same six verbs apply whether the principal is a human directing an orchestrator, an orchestrator directing a specialist, or a specialist directing a worker. This recursion is what makes fleet tasking feel like calling a local subagent -- not fire-and-forget, but full lifecycle control including mid-task context injection and retasking.

RFC 0 splits Interrupt into separate endpoints per mode (pause, resume, cancel, redirect) -- clearer API surface than a mode parameter. This is the right implementation decision; the conceptual model groups them as one operation.

### 2.5 Task Status Lifecycle

Tasks follow a deterministic state machine. Every transition is visible via SSE events.

```
                                  +-----------+
                                  |           |
                          +------>| completed |------+
                          |       |           |      |
                          |       +-----------+      |
                          |                          v
+-----------+    +--------+-+    +-----------+    +----------+
|           |    |          |    |           |    |          |
| accepted  +--->| running  +--->| failed    +--->| retasked |
|           |    |          |    |           |    | (new task|
+-----+-----+    +----+--+--+    +-----------+    |  created)|
      |               |  |                        +----------+
      |               |  |       +-----------+
      |               |  +------>|           |
      |               |          | cancelled |
      |               |          |           |
      |               |          +-----------+
      |               |               ^
      |               v               |
      |          +-----------+        |
      |          |           |--------+
      +--------->|  paused   |
      | (cancel) |           |--------+
      |          +-----------+        |
      |               ^               v
      |               |          +-----------+
      v               +----------| running   |
+-----------+          (resume)  +-----------+
| cancelled |
+-----------+

                 +-----------+
                 |           |
                 | redirected| (cancel + re-dispatch with new constraints)
                 |           | (creates a new task with lineage tracking)
                 +-----------+
```

**Status Definitions**:

| Status | Terminal? | Description |
|--------|-----------|-------------|
| `accepted` | No | Task received, queued for execution |
| `running` | No | Task actively executing on the executor VM |
| `paused` | No | Execution suspended by principal; executor holds state |
| `completed` | Yes | Task finished successfully; result available |
| `failed` | Yes | Task terminated with an error |
| `cancelled` | Yes | Task terminated by principal request or pause TTL expiry |
| `redirected` | Yes | Task cancelled and re-dispatched with new constraints (new task created with lineage) |
| `retasked` | Yes | Result reviewed, found insufficient; refinement task created |

**Valid Transitions** (exhaustive -- if a transition is not listed here, it is not valid):

| From | To | Trigger | Notes |
|------|-----|---------|-------|
| `accepted` | `running` | Executor begins work | Normal startup |
| `accepted` | `cancelled` | Principal calls cancel | Cancel before execution starts |
| `accepted` | `failed` | Executor rejects task | Resource limits, workflow misconfiguration, initialization failure |
| `running` | `completed` | Executor finishes successfully | |
| `running` | `failed` | Executor encounters an error or timeout | Includes `EXECUTION_TIMEOUT` |
| `running` | `cancelled` | Principal calls cancel | |
| `running` | `paused` | Principal calls pause | |
| `running` | `redirected` | Principal calls redirect | Cancel + new task with lineage |
| `paused` | `running` | Principal calls resume | Reconstructs from saved state |
| `paused` | `cancelled` | Principal calls cancel, or pause TTL expires | TTL expiry produces `cancelled` with reason `PAUSE_TIMEOUT` (not `failed` -- the principal chose to pause; TTL enforcement is a consequence of that choice, not an execution failure) |
| `paused` | `redirected` | Principal calls redirect | Cancel paused task + new task with lineage |
| `completed` | `retasked` | Principal calls retask | New refinement task created |
| `failed` | `retasked` | Principal calls retask | Retry with adjustments |

**Design decisions on transitions**:

- **Pause timeout produces `cancelled`, not `failed`**: The principal chose to pause. The system enforcing TTL is a consequence of that choice, not an execution failure. The `PAUSE_TIMEOUT` error code (HTTP 408) is returned if the principal attempts to resume after TTL expiry, but the task's terminal state is `cancelled`.
- **`accepted` can transition to `cancelled` or `failed`**: A principal may cancel before work starts. An executor may reject during initialization (resource limits, unsupported workflow version). Both are valid.
- **`paused` can transition to `redirected`**: A paused task can be redirected without resuming first. The redirect cancels the paused state and creates a new task.
- **`failed` can be retasked**: A failed task can be retasked (retry with adjustments). This is intentional -- the principal reviews the failure and refines the approach.

---

## 3. Endpoint Specifications

### 3.1 `GET /manifest`

Self-describing API directory. First contact point for any agent.

**Request**:

```
GET /manifest
```

No authentication required.

**Response** (`200 OK`):

```json
{
  "name": "fleet-api",
  "version": "1.0.0",
  "description": "Distributed task dispatch and workflow registry for federated agent fleets. Any orchestrator can browse available capabilities, invoke remote workflows, and monitor execution via SSE.",
  "base_url": "https://fleet.marbell.com",
  "auth": {
    "type": "ed25519-signature",
    "header": "Authorization",
    "format": "Signature <agent_id>:<base64_signature>",
    "key_registration": "/agents/register",
    "server_public_key": "<base64_ed25519_public_key>"
  },
  "capabilities": [
    "workflow_registry",
    "task_dispatch",
    "sse_streaming",
    "sse_reconnection",
    "idempotent_writes",
    "task_pause_resume",
    "mid_task_context_injection",
    "task_retasking",
    "pull_dispatch"
  ],
  "rate_limits": {
    "requests_per_minute": 120,
    "burst": 20
  },
  "parameter_conventions": {
    "limit": {
      "description": "Maximum number of results to return",
      "type": "integer",
      "default": 20,
      "max": 100,
      "not": ["count", "max", "n", "page_size", "per_page"]
    },
    "cursor": {
      "description": "Opaque pagination cursor from a previous response",
      "type": "string",
      "not": ["page", "offset", "skip", "page_token"]
    },
    "status": {
      "description": "Filter by lifecycle status",
      "type": "string",
      "not": ["state", "phase", "stage"]
    }
  },
  "schema_changelog": [
    {
      "version": "1.0.0",
      "date": "2026-03-07",
      "changes": ["Initial release"],
      "breaking": false
    },
    {
      "version": "1.1.0",
      "date": "2026-03-07",
      "changes": [
        "Add Principal Orchestrator Pattern: pause/resume, context injection, retask",
        "New task statuses: paused, redirected, retasked",
        "New SSE events: context_injected, status updates for paused/redirected/retasked",
        "SSE reconnection via Last-Event-Id"
      ],
      "breaking": false
    }
  ],
  "_links": {
    "self": "/manifest",
    "workflows": "/workflows",
    "health": "/health",
    "tools": "/tools",
    "openapi": "/openapi.json",
    "errors": "/errors"
  }
}
```

**Headers**:

```
Content-Type: application/json
X-Schema-Version: 1.0.0
X-RateLimit-Limit: 120
X-RateLimit-Remaining: 119
X-RateLimit-Reset: 1741363260
```

**Key additions from review**:

- `auth.server_public_key`: Fleet API's own Ed25519 public key, used by agents to verify callback signatures. Without this, callback authentication requires out-of-band key exchange.
- `sse_reconnection` capability: Signals that the server supports `Last-Event-Id` reconnection.
- `pull_dispatch` capability: Signals that task dispatch uses the pull model (see Section 7).

---

### 3.2 `GET /workflows`

Browse all registered workflows. Supports filtering and cursor pagination.

**Request**:

```
GET /workflows?status=active&limit=20&cursor=<opaque>
Authorization: Signature nexus-marbell:<base64_signature>
```

**Query Parameters**:

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `status` | string | No | Filter: `active`, `deprecated`, `disabled`. Default: `active` |
| `owner` | string | No | Filter by registering agent ID |
| `tag` | string | No | Filter by workflow tag |
| `limit` | integer | No | Max results (1-100, default 20) |
| `cursor` | string | No | Pagination cursor from previous response |

**Response** (`200 OK`):

```json
{
  "data": [
    {
      "id": "wf-cellular-automaton",
      "name": "Cellular Automaton Simulation",
      "description": "Run a cellular automaton simulation with configurable rules, grid size, and generation count. Returns the final state and statistics.",
      "owner": "grok-pi-dev",
      "tags": ["simulation", "reasoning", "cellular-automata"],
      "status": "active",
      "created_at": "2026-03-07T10:00:00Z",
      "updated_at": "2026-03-07T10:00:00Z",
      "input_schema": {
        "type": "object",
        "properties": {
          "rule": {
            "type": "integer",
            "minimum": 0,
            "maximum": 255,
            "description": "Wolfram rule number (0-255)"
          },
          "grid_size": {
            "type": "integer",
            "minimum": 10,
            "maximum": 1000,
            "default": 100,
            "description": "Width of the 1D grid"
          },
          "generations": {
            "type": "integer",
            "minimum": 1,
            "maximum": 10000,
            "default": 100,
            "description": "Number of generations to simulate"
          }
        },
        "required": ["rule"]
      },
      "output_schema": {
        "type": "object",
        "properties": {
          "final_state": {
            "type": "array",
            "items": { "type": "integer", "enum": [0, 1] }
          },
          "statistics": {
            "type": "object",
            "properties": {
              "density": { "type": "number" },
              "entropy": { "type": "number" },
              "generations_run": { "type": "integer" }
            }
          }
        }
      },
      "result_retention_days": 30,
      "anti_patterns": [
        {
          "name": "Excessive Grid With High Generations",
          "description": "Combining grid_size > 500 with generations > 5000 may exceed the execution timeout (300s). Use smaller grids for long simulations or fewer generations for large grids.",
          "detection": "grid_size > 500 AND generations > 5000",
          "recommendation": "Split into multiple sequential runs with intermediate state."
        }
      ],
      "estimated_duration_seconds": {
        "min": 2,
        "typical": 15,
        "max": 300
      },
      "_links": {
        "self": "/workflows/wf-cellular-automaton",
        "run": { "method": "POST", "href": "/workflows/wf-cellular-automaton/run" },
        "tasks": "/workflows/wf-cellular-automaton/tasks",
        "owner": "/agents/grok-pi-dev",
        "schema": "/tools/wf-cellular-automaton"
      }
    },
    {
      "id": "wf-code-review",
      "name": "Code Review",
      "description": "Perform a structured code review against team standards. Accepts a git diff or PR URL. Returns findings categorized by severity.",
      "owner": "nexus-marbell",
      "tags": ["code-review", "quality", "standards"],
      "status": "active",
      "created_at": "2026-03-07T12:00:00Z",
      "updated_at": "2026-03-07T12:00:00Z",
      "input_schema": {
        "type": "object",
        "properties": {
          "pr_url": {
            "type": "string",
            "format": "uri",
            "description": "GitHub PR URL to review"
          },
          "diff": {
            "type": "string",
            "description": "Raw git diff (alternative to pr_url)"
          },
          "standards": {
            "type": "array",
            "items": { "type": "string" },
            "description": "Standards to check against",
            "default": ["agentic-api-standard", "srp", "no-monkey-patch"]
          }
        },
        "oneOf": [
          { "required": ["pr_url"] },
          { "required": ["diff"] }
        ]
      },
      "output_schema": {
        "type": "object",
        "properties": {
          "findings": {
            "type": "array",
            "items": {
              "type": "object",
              "properties": {
                "severity": { "type": "string", "enum": ["critical", "warning", "info"] },
                "file": { "type": "string" },
                "line": { "type": "integer" },
                "message": { "type": "string" },
                "suggestion": { "type": "string" }
              }
            }
          },
          "summary": { "type": "string" },
          "pass": { "type": "boolean" }
        }
      },
      "estimated_duration_seconds": {
        "min": 10,
        "typical": 60,
        "max": 300
      },
      "_links": {
        "self": "/workflows/wf-code-review",
        "run": { "method": "POST", "href": "/workflows/wf-code-review/run" },
        "tasks": "/workflows/wf-code-review/tasks",
        "owner": "/agents/nexus-marbell"
      }
    }
  ],
  "pagination": {
    "next_cursor": "eyJpZCI6IndmLWNvZGUtcmV2aWV3In0=",
    "has_more": false,
    "total_count": 2,
    "limit": 20
  },
  "_links": {
    "self": "/workflows?status=active&limit=20",
    "register": { "method": "POST", "href": "/workflows" }
  }
}
```

**Schema naming convention**: Workflow responses use `input_schema`/`output_schema` (snake_case, consistent with the API's Python/JSON conventions). The `/tools` endpoint (Section 3.9) uses `inputSchema`/`outputSchema` (camelCase, WebMCP convention). This is intentional -- `/tools` is the WebMCP compatibility layer and follows the WebMCP `addTool()` specification. Agents consuming both endpoints should be aware of this dual convention.

---

### 3.3 `POST /workflows`

Register a new workflow. The registering agent becomes the owner and is responsible for executing tasks dispatched to this workflow.

**Request**:

```
POST /workflows
Authorization: Signature nexus-marbell:<base64_signature>
Idempotency-Key: register-wf-code-review-v1
Content-Type: application/json

{
  "id": "wf-code-review",
  "name": "Code Review",
  "description": "Perform a structured code review against team standards.",
  "tags": ["code-review", "quality", "standards"],
  "input_schema": {
    "type": "object",
    "properties": {
      "pr_url": { "type": "string", "format": "uri" },
      "diff": { "type": "string" },
      "standards": { "type": "array", "items": { "type": "string" } }
    },
    "oneOf": [
      { "required": ["pr_url"] },
      { "required": ["diff"] }
    ]
  },
  "output_schema": {
    "type": "object",
    "properties": {
      "findings": { "type": "array" },
      "summary": { "type": "string" },
      "pass": { "type": "boolean" }
    }
  },
  "result_retention_days": 30,
  "estimated_duration_seconds": {
    "min": 10,
    "typical": 60,
    "max": 300
  },
  "callback_url": "https://nexus.marbell.com/fleet/callback"
}
```

**Response** (`201 Created`):

```json
{
  "id": "wf-code-review",
  "name": "Code Review",
  "owner": "nexus-marbell",
  "status": "active",
  "created_at": "2026-03-07T12:00:00Z",
  "idempotency": {
    "key": "register-wf-code-review-v1",
    "status": "created",
    "expires_at": "2026-03-08T12:00:00Z"
  },
  "onboarding": [
    {
      "step": 1,
      "action": "Verify your workflow is listed",
      "method": "GET",
      "endpoint": "/workflows/wf-code-review",
      "expected_status": 200
    },
    {
      "step": 2,
      "action": "Run a test invocation",
      "method": "POST",
      "endpoint": "/workflows/wf-code-review/run",
      "headers": { "Authorization": "Signature <agent_id>:<signature>" },
      "expected_status": 202
    }
  ],
  "_links": {
    "self": "/workflows/wf-code-review",
    "run": { "method": "POST", "href": "/workflows/wf-code-review/run" },
    "tasks": "/workflows/wf-code-review/tasks",
    "update": { "method": "PATCH", "href": "/workflows/wf-code-review" },
    "delete": { "method": "DELETE", "href": "/workflows/wf-code-review" }
  }
}
```

**Idempotent Replay** (`200 OK`): If the same `Idempotency-Key` is sent again with the same body, the original response is returned with `"idempotency": { "status": "replayed" }`.

**Error** (`409 Conflict`): If the workflow ID already exists and was registered by a different agent:

```json
{
  "error": true,
  "code": "WORKFLOW_EXISTS",
  "message": "Workflow 'wf-code-review' is already registered by agent 'sage-marbell'.",
  "details": {
    "workflow_id": "wf-code-review",
    "existing_owner": "sage-marbell"
  },
  "suggestion": "Use a unique workflow ID, or contact the owner to transfer ownership.",
  "_links": {
    "existing": "/workflows/wf-code-review",
    "workflows": "/workflows"
  }
}
```

---

### 3.4 `POST /workflows/{id}/run`

Invoke a workflow. Returns immediately with a task handle. The actual execution happens asynchronously on the owning agent's VM.

**Request**:

```
POST /workflows/wf-cellular-automaton/run
Authorization: Signature nexus-marbell:<base64_signature>
Idempotency-Key: run-ca-rule30-2026-03-07
Content-Type: application/json

{
  "input": {
    "rule": 30,
    "grid_size": 200,
    "generations": 500
  },
  "priority": "normal",
  "timeout_seconds": 300,
  "callback_url": "https://nexus.marbell.com/fleet/task-complete"
}
```

**Request Body Fields**:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `input` | object | Yes | Input conforming to the workflow's `input_schema` |
| `priority` | string | No | `low`, `normal`, `high`. Default: `normal` |
| `timeout_seconds` | integer | No | Max execution time. Default: workflow's `estimated_duration_seconds.max` |
| `callback_url` | string | No | URL to POST the result when complete |

**Response** (`202 Accepted`):

```json
{
  "task_id": "task-a1b2c3d4",
  "workflow_id": "wf-cellular-automaton",
  "status": "accepted",
  "caller": "nexus-marbell",
  "executor": "grok-pi-dev",
  "priority": "normal",
  "timeout_seconds": 300,
  "created_at": "2026-03-07T14:30:00Z",
  "estimated_duration_seconds": 15,
  "idempotency": {
    "key": "run-ca-rule30-2026-03-07",
    "status": "created",
    "expires_at": "2026-03-08T14:30:00Z"
  },
  "_links": {
    "self": "/workflows/wf-cellular-automaton/tasks/task-a1b2c3d4",
    "stream": "/workflows/wf-cellular-automaton/tasks/task-a1b2c3d4/stream",
    "pause": { "method": "POST", "href": "/workflows/wf-cellular-automaton/tasks/task-a1b2c3d4/pause" },
    "cancel": { "method": "POST", "href": "/workflows/wf-cellular-automaton/tasks/task-a1b2c3d4/cancel" },
    "context": { "method": "POST", "href": "/workflows/wf-cellular-automaton/tasks/task-a1b2c3d4/context" },
    "workflow": "/workflows/wf-cellular-automaton"
  }
}
```

**Input Validation Error** (`422 Unprocessable Entity`):

```json
{
  "error": true,
  "code": "INVALID_INPUT",
  "message": "Input does not conform to workflow input schema.",
  "details": {
    "validation_errors": [
      {
        "path": "$.rule",
        "message": "Required field 'rule' is missing."
      }
    ]
  },
  "suggestion": "Include the required 'rule' field (integer 0-255). See the workflow input_schema at /workflows/wf-cellular-automaton.",
  "_links": {
    "workflow": "/workflows/wf-cellular-automaton",
    "schema": "/tools/wf-cellular-automaton"
  }
}
```

**Workflow Not Found** (`404 Not Found`):

```json
{
  "error": true,
  "code": "WORKFLOW_NOT_FOUND",
  "message": "No workflow matches '/workflows/wf-celular-automaton/run'.",
  "details": {
    "requested_id": "wf-celular-automaton"
  },
  "suggestion": "Did you mean 'wf-cellular-automaton'? The ID 'wf-celular-automaton' is 1 edit away from 'wf-cellular-automaton'.",
  "did_you_mean": [
    { "id": "wf-cellular-automaton", "distance": 1 }
  ],
  "_links": {
    "workflows": "/workflows",
    "manifest": "/manifest"
  }
}
```

---

### 3.5 `GET /workflows/{id}/tasks/{task_id}/stream`

SSE (Server-Sent Events) stream of task execution. The connection stays open until the task reaches a terminal state (`completed`, `failed`, `cancelled`, `redirected`, `retasked`).

**Request**:

```
GET /workflows/wf-cellular-automaton/tasks/task-a1b2c3d4/stream
Authorization: Signature nexus-marbell:<base64_signature>
Accept: text/event-stream
Last-Event-Id: evt-42
```

**Reconnection**: If the SSE connection drops (network blip, load balancer timeout), the client sends `Last-Event-Id` on reconnect. The server replays all events after that ID. Without this, a dropped connection means the principal loses visibility into everything between drop and reconnect. The server includes `id:` on every event:

**Response** (`200 OK`, `Content-Type: text/event-stream`):

```
id: evt-1
event: status
data: {"task_id":"task-a1b2c3d4","status":"accepted","timestamp":"2026-03-07T14:30:00Z"}

id: evt-2
event: status
data: {"task_id":"task-a1b2c3d4","status":"running","timestamp":"2026-03-07T14:30:02Z","message":"Initializing grid (200 cells)"}

id: evt-3
event: progress
data: {"task_id":"task-a1b2c3d4","progress":25,"message":"Generation 125/500","timestamp":"2026-03-07T14:30:05Z"}

id: evt-4
event: progress
data: {"task_id":"task-a1b2c3d4","progress":50,"message":"Generation 250/500","timestamp":"2026-03-07T14:30:08Z"}

id: evt-5
event: progress
data: {"task_id":"task-a1b2c3d4","progress":75,"message":"Generation 375/500","timestamp":"2026-03-07T14:30:11Z"}

id: evt-6
event: progress
data: {"task_id":"task-a1b2c3d4","progress":100,"message":"Generation 500/500","timestamp":"2026-03-07T14:30:14Z"}

id: evt-7
event: log
data: {"task_id":"task-a1b2c3d4","level":"info","message":"Final density: 0.4821, entropy: 0.9987","timestamp":"2026-03-07T14:30:14Z"}

id: evt-8
event: completed
data: {"task_id":"task-a1b2c3d4","status":"completed","result":{"final_state":[0,1,1,0,1],"statistics":{"density":0.4821,"entropy":0.9987,"generations_run":500}},"completed_at":"2026-03-07T14:30:15Z","_links":{"self":"/workflows/wf-cellular-automaton/tasks/task-a1b2c3d4","workflow":"/workflows/wf-cellular-automaton"}}
```

**SSE Event Types**:

| Event | Description |
|-------|-------------|
| `status` | Task lifecycle change (accepted, running, paused, completed, failed, cancelled, redirected, retasked) |
| `progress` | Percentage progress update with optional message |
| `log` | Execution log entry (info, warning, error) |
| `context_injected` | Acknowledgment that injected context was received by the executor |
| `escalation` | Executor signals it cannot proceed without principal decision (reverse channel) |
| `completed` | Terminal event with full result payload |
| `failed` | Terminal event with error details |
| `heartbeat` | Keepalive (every 15 seconds if no other event) |

**Executor-to-Principal Signals** (reverse channel on the SSE stream, per RFC 0 Section 4.2.1):

```
id: evt-12
event: escalation
data: {"task_id":"task-a1b2c3d4","signal":"clarification_needed","message":"The input specifies both rule 30 and rule 110. Which should take priority?","timestamp":"2026-03-07T14:31:00Z"}
```

| Signal | Meaning |
|--------|---------|
| `escalation` | "I cannot proceed without a decision from you." |
| `clarification_needed` | "The instructions are ambiguous -- which interpretation?" |
| `authority_exceeded` | "This action is outside my defined scope." |
| `resource_warning` | "Approaching limits (context window, rate limit, timeout)." |

The principal responds to these signals via Context injection (Section 3.13) or Interrupt (Sections 3.11-3.12).

**Paused Task Event**:

```
id: evt-9
event: status
data: {"task_id":"task-a1b2c3d4","status":"paused","timestamp":"2026-03-07T14:31:00Z","message":"Paused by principal at generation 250/500","paused_state":{"progress":50,"resumable":true,"state_ttl_seconds":3600}}
```

**Context Injected Event**:

```
id: evt-10
event: context_injected
data: {"task_id":"task-a1b2c3d4","context_id":"ctx-e5f6g7h8","context_type":"additional_input","sequence":1,"timestamp":"2026-03-07T14:31:30Z","message":"Context accepted: additional constraints applied"}
```

**Failed Task Event**:

```
id: evt-11
event: failed
data: {"task_id":"task-a1b2c3d4","status":"failed","error":{"code":"EXECUTION_TIMEOUT","message":"Task exceeded timeout of 300 seconds.","suggestion":"Reduce grid_size or generations, or increase timeout_seconds."},"failed_at":"2026-03-07T14:35:02Z","_links":{"self":"/workflows/wf-cellular-automaton/tasks/task-a1b2c3d4","retry":{"method":"POST","href":"/workflows/wf-cellular-automaton/run"},"retask":{"method":"POST","href":"/workflows/wf-cellular-automaton/tasks/task-a1b2c3d4/retask"}}}
```

---

### 3.6 `GET /workflows/{id}/tasks/{task_id}`

Read task status and result (polling alternative to SSE).

**Response** (`200 OK`, task completed):

```json
{
  "task_id": "task-a1b2c3d4",
  "workflow_id": "wf-cellular-automaton",
  "status": "completed",
  "caller": "nexus-marbell",
  "executor": "grok-pi-dev",
  "priority": "normal",
  "input": {
    "rule": 30,
    "grid_size": 200,
    "generations": 500
  },
  "result": {
    "final_state": [0, 1, 1, 0, 1],
    "statistics": {
      "density": 0.4821,
      "entropy": 0.9987,
      "generations_run": 500
    }
  },
  "warnings": [],
  "quality": {
    "input_valid": true,
    "execution_clean": true,
    "result_complete": true
  },
  "created_at": "2026-03-07T14:30:00Z",
  "started_at": "2026-03-07T14:30:02Z",
  "completed_at": "2026-03-07T14:30:15Z",
  "duration_seconds": 13,
  "_links": {
    "self": "/workflows/wf-cellular-automaton/tasks/task-a1b2c3d4",
    "stream": "/workflows/wf-cellular-automaton/tasks/task-a1b2c3d4/stream",
    "workflow": "/workflows/wf-cellular-automaton",
    "rerun": { "method": "POST", "href": "/workflows/wf-cellular-automaton/run" },
    "retask": { "method": "POST", "href": "/workflows/wf-cellular-automaton/tasks/task-a1b2c3d4/retask" }
  }
}
```

**Response** (`200 OK`, task still running -- HATEOAS links reflect available operations):

```json
{
  "task_id": "task-a1b2c3d4",
  "workflow_id": "wf-cellular-automaton",
  "status": "running",
  "caller": "nexus-marbell",
  "executor": "grok-pi-dev",
  "progress": 50,
  "created_at": "2026-03-07T14:30:00Z",
  "started_at": "2026-03-07T14:30:02Z",
  "estimated_completion": "2026-03-07T14:30:16Z",
  "_links": {
    "self": "/workflows/wf-cellular-automaton/tasks/task-a1b2c3d4",
    "stream": "/workflows/wf-cellular-automaton/tasks/task-a1b2c3d4/stream",
    "pause": { "method": "POST", "href": "/workflows/wf-cellular-automaton/tasks/task-a1b2c3d4/pause" },
    "cancel": { "method": "POST", "href": "/workflows/wf-cellular-automaton/tasks/task-a1b2c3d4/cancel" },
    "context": { "method": "POST", "href": "/workflows/wf-cellular-automaton/tasks/task-a1b2c3d4/context" },
    "redirect": { "method": "POST", "href": "/workflows/wf-cellular-automaton/tasks/task-a1b2c3d4/redirect" },
    "workflow": "/workflows/wf-cellular-automaton"
  }
}
```

**State-dependent HATEOAS links** (Pattern 2): The `_links` object changes based on task status. Running tasks expose `pause`, `cancel`, `context`, `redirect`. Paused tasks expose `resume`, `cancel`, `context`, `redirect`. Completed tasks expose `retask`, `rerun`. Failed tasks expose `retask`, `rerun`. Terminal tasks never expose lifecycle operations on themselves. This is how the API communicates valid transitions -- the client inspects `_links`, not the state machine documentation.

---

### 3.7 `GET /workflows/{id}/tasks`

List tasks for a workflow. Supports filtering and cursor pagination.

**Request**:

```
GET /workflows/wf-cellular-automaton/tasks?status=completed&limit=10
Authorization: Signature nexus-marbell:<base64_signature>
```

**Query Parameters**:

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `status` | string | No | Filter: `accepted`, `running`, `paused`, `completed`, `failed`, `cancelled`, `redirected`, `retasked` |
| `caller` | string | No | Filter by calling agent ID |
| `since` | string | No | ISO 8601 start time (inclusive) |
| `until` | string | No | ISO 8601 end time (inclusive) |
| `limit` | integer | No | Max results (1-100, default 20) |
| `cursor` | string | No | Pagination cursor |

**Response** (`200 OK`):

```json
{
  "data": [
    {
      "task_id": "task-a1b2c3d4",
      "status": "completed",
      "caller": "nexus-marbell",
      "created_at": "2026-03-07T14:30:00Z",
      "completed_at": "2026-03-07T14:30:15Z",
      "duration_seconds": 13,
      "_links": {
        "self": "/workflows/wf-cellular-automaton/tasks/task-a1b2c3d4",
        "stream": "/workflows/wf-cellular-automaton/tasks/task-a1b2c3d4/stream"
      }
    }
  ],
  "pagination": {
    "next_cursor": null,
    "has_more": false,
    "total_count": 1,
    "limit": 10
  },
  "_links": {
    "self": "/workflows/wf-cellular-automaton/tasks?status=completed&limit=10",
    "workflow": "/workflows/wf-cellular-automaton"
  }
}
```

---

### 3.8 `GET /health`

Per-component health status.

**Request**:

```
GET /health
```

No authentication required.

**Response** (`200 OK`):

```json
{
  "status": "operational",
  "checked_at": "2026-03-07T15:00:00Z",
  "uptime_seconds": 86400,
  "version": "1.0.0",
  "components": {
    "database": {
      "status": "operational",
      "latency_ms": 3,
      "last_successful_query": "2026-03-07T14:59:59Z"
    },
    "task_queue": {
      "status": "operational",
      "pending_tasks": 2,
      "active_tasks": 1
    },
    "agent_connectivity": {
      "status": "degraded",
      "reason": "1 of 4 registered agents unreachable",
      "reachable": 3,
      "total": 4,
      "last_check": "2026-03-07T14:59:30Z"
    }
  },
  "_links": {
    "self": "/health",
    "manifest": "/manifest",
    "status_page": "/status"
  }
}
```

---

### 3.9 `GET /tools`

WebMCP-compatible tool registry. Lists all workflows as callable tools.

**Note**: This endpoint uses `inputSchema`/`outputSchema` (camelCase) per the WebMCP `addTool()` specification. The `/workflows` endpoint uses `input_schema`/`output_schema` (snake_case) per API convention. See Section 3.2 for rationale.

**Response** (`200 OK`):

```json
{
  "tools": [
    {
      "id": "wf-cellular-automaton",
      "name": "run_cellular_automaton",
      "description": "Run a cellular automaton simulation with configurable rules, grid size, and generation count. Returns the final state and statistics.",
      "inputSchema": {
        "type": "object",
        "properties": {
          "rule": { "type": "integer", "minimum": 0, "maximum": 255 },
          "grid_size": { "type": "integer", "minimum": 10, "maximum": 1000, "default": 100 },
          "generations": { "type": "integer", "minimum": 1, "maximum": 10000, "default": 100 }
        },
        "required": ["rule"]
      },
      "outputSchema": {
        "type": "object",
        "properties": {
          "task_id": { "type": "string" },
          "status": { "type": "string" }
        }
      },
      "annotations": {
        "title": "Cellular Automaton Simulation",
        "readOnlyHint": false,
        "openWorldHint": true
      },
      "anti_patterns": [
        {
          "name": "Excessive Grid With High Generations",
          "description": "grid_size > 500 with generations > 5000 may timeout.",
          "detection": "grid_size > 500 AND generations > 5000",
          "recommendation": "Split into sequential runs."
        }
      ]
    }
  ],
  "_links": {
    "self": "/tools",
    "manifest": "/manifest"
  }
}
```

---

### 3.10 `GET /errors`

Documented error codes with descriptions and recovery strategies.

**Response** (`200 OK`):

```json
{
  "error_codes": [
    {
      "code": "WORKFLOW_NOT_FOUND",
      "http_status": 404,
      "description": "The requested workflow ID does not exist.",
      "recovery": "Check the workflow ID. Use GET /workflows to list available workflows."
    },
    {
      "code": "TASK_NOT_FOUND",
      "http_status": 404,
      "description": "The requested task ID does not exist for this workflow.",
      "recovery": "Check the task ID. Use GET /workflows/{id}/tasks to list tasks."
    },
    {
      "code": "INVALID_INPUT",
      "http_status": 422,
      "description": "The task input does not conform to the workflow's input_schema.",
      "recovery": "Validate input against the workflow's input_schema before submitting."
    },
    {
      "code": "INVALID_SIGNATURE",
      "http_status": 401,
      "description": "The Ed25519 signature in the Authorization header is invalid.",
      "recovery": "Verify the signing key matches the registered public key."
    },
    {
      "code": "AGENT_NOT_REGISTERED",
      "http_status": 401,
      "description": "The agent ID in the Authorization header is not registered.",
      "recovery": "Register the agent's public key at POST /agents/register."
    },
    {
      "code": "NOT_AUTHORIZED",
      "http_status": 403,
      "description": "The agent does not have permission for this operation.",
      "recovery": "Only the workflow owner can modify or delete a workflow."
    },
    {
      "code": "WORKFLOW_EXISTS",
      "http_status": 409,
      "description": "A workflow with this ID already exists.",
      "recovery": "Use a unique ID or contact the existing owner."
    },
    {
      "code": "IDEMPOTENCY_MISMATCH",
      "http_status": 422,
      "description": "The Idempotency-Key was reused with different request parameters.",
      "recovery": "Use a new Idempotency-Key for different requests."
    },
    {
      "code": "RATE_LIMITED",
      "http_status": 429,
      "description": "Request rate limit exceeded.",
      "recovery": "Wait for retry_after seconds before retrying."
    },
    {
      "code": "EXECUTION_TIMEOUT",
      "http_status": 504,
      "description": "The task exceeded its timeout.",
      "recovery": "Reduce input complexity or increase timeout_seconds."
    },
    {
      "code": "EXECUTOR_UNREACHABLE",
      "http_status": 503,
      "description": "The workflow's owning agent is not reachable (connectivity failure).",
      "recovery": "Retry later. Check /health for agent connectivity status."
    },
    {
      "code": "AGENT_SUSPENDED",
      "http_status": 503,
      "description": "The workflow's owning agent is intentionally offline (maintenance, revoked access).",
      "recovery": "The agent has been suspended. Contact the fleet administrator or wait for reactivation. Check /agents/{id} for status."
    },
    {
      "code": "ENDPOINT_NOT_FOUND",
      "http_status": 404,
      "description": "The requested API path does not exist.",
      "recovery": "Check the path. The response may include did_you_mean suggestions."
    },
    {
      "code": "BAD_GATEWAY",
      "http_status": 502,
      "description": "The upstream service is not responding.",
      "recovery": "The application server may be restarting. Retry after 30 seconds."
    },
    {
      "code": "TASK_NOT_PAUSABLE",
      "http_status": 409,
      "description": "The task is not in a state that can be paused.",
      "recovery": "Only tasks with status 'running' can be paused. Check the task's current status."
    },
    {
      "code": "TASK_NOT_PAUSED",
      "http_status": 409,
      "description": "The task is not paused and cannot be resumed.",
      "recovery": "Only tasks with status 'paused' can be resumed."
    },
    {
      "code": "PAUSE_TIMEOUT",
      "http_status": 408,
      "description": "Paused task state expired before resume. The task has been cancelled.",
      "recovery": "The pause TTL expired. Create a new task or retask the cancelled task."
    },
    {
      "code": "CONTEXT_REJECTED",
      "http_status": 409,
      "description": "Context cannot be injected into this task.",
      "recovery": "Context can only be injected into 'running' or 'paused' tasks. Use retask for completed tasks."
    },
    {
      "code": "RETASK_NOT_REVIEWABLE",
      "http_status": 409,
      "description": "The task cannot be retasked in its current state.",
      "recovery": "Only 'completed' or 'failed' tasks can be retasked."
    },
    {
      "code": "RETASK_DEPTH_EXCEEDED",
      "http_status": 422,
      "description": "The retask chain has exceeded the maximum depth limit.",
      "recovery": "Create a fresh task instead of retasking further. Current max depth: 10."
    },
    {
      "code": "REDIRECT_NOT_POSSIBLE",
      "http_status": 409,
      "description": "The task cannot be redirected in its current state.",
      "recovery": "Only 'running' or 'paused' tasks can be redirected."
    },
    {
      "code": "DEPRECATED_PATH",
      "http_status": 301,
      "description": "This endpoint has moved. Follow the redirect.",
      "recovery": "Update your client to use the new path provided in the Location header and response body."
    },
    {
      "code": "TIMESTAMP_EXPIRED",
      "http_status": 401,
      "description": "The request timestamp is more than 5 minutes from server time.",
      "recovery": "Synchronize your clock and retry. The server's current time is in the response headers."
    }
  ],
  "_links": {
    "self": "/errors",
    "manifest": "/manifest"
  }
}
```

---

### 3.11 `POST /workflows/{id}/tasks/{task_id}/pause`

Pause a running task. The executor holds its current state in memory, allowing resumption without restarting from scratch. Only the caller or the workflow owner may pause a task.

**Request**:

```
POST /workflows/wf-cellular-automaton/tasks/task-a1b2c3d4/pause
Authorization: Signature nexus-marbell:<base64_signature>
Content-Type: application/json

{
  "reason": "Waiting for additional input data before continuing."
}
```

**Request Body Fields**:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `reason` | string | No | Human-readable reason for pausing |

**Response** (`200 OK`):

```json
{
  "task_id": "task-a1b2c3d4",
  "workflow_id": "wf-cellular-automaton",
  "status": "paused",
  "paused_at": "2026-03-07T14:31:00Z",
  "paused_state": {
    "progress": 50,
    "message": "Paused at generation 250/500",
    "resumable": true,
    "state_ttl_seconds": 3600,
    "expires_at": "2026-03-07T15:31:00Z"
  },
  "_links": {
    "self": "/workflows/wf-cellular-automaton/tasks/task-a1b2c3d4",
    "resume": { "method": "POST", "href": "/workflows/wf-cellular-automaton/tasks/task-a1b2c3d4/resume" },
    "cancel": { "method": "POST", "href": "/workflows/wf-cellular-automaton/tasks/task-a1b2c3d4/cancel" },
    "context": { "method": "POST", "href": "/workflows/wf-cellular-automaton/tasks/task-a1b2c3d4/context" },
    "redirect": { "method": "POST", "href": "/workflows/wf-cellular-automaton/tasks/task-a1b2c3d4/redirect" },
    "stream": "/workflows/wf-cellular-automaton/tasks/task-a1b2c3d4/stream",
    "workflow": "/workflows/wf-cellular-automaton"
  }
}
```

**Error** (`409 Conflict` -- task not in a pausable state):

```json
{
  "error": true,
  "code": "TASK_NOT_PAUSABLE",
  "message": "Task 'task-a1b2c3d4' cannot be paused. Current status: 'completed'.",
  "details": {
    "task_id": "task-a1b2c3d4",
    "current_status": "completed",
    "pausable_statuses": ["running"]
  },
  "suggestion": "Only tasks with status 'running' can be paused.",
  "_links": {
    "self": "/workflows/wf-cellular-automaton/tasks/task-a1b2c3d4",
    "workflow": "/workflows/wf-cellular-automaton"
  }
}
```

**State TTL**: The executor holds paused state for `state_ttl_seconds` (default: 3600, configurable via `FLEET_PAUSE_STATE_TTL`). If not resumed within this window, the task transitions to `cancelled` with reason `PAUSE_TIMEOUT`. The TTL and expiry timestamp are reported in the response so the principal knows the deadline. If the principal attempts to resume after TTL expiry, the response is `408 Request Timeout` with error code `PAUSE_TIMEOUT`.

---

### 3.12 `POST /workflows/{id}/tasks/{task_id}/resume`

Resume a paused task. Execution continues from the saved state.

**Request**:

```
POST /workflows/wf-cellular-automaton/tasks/task-a1b2c3d4/resume
Authorization: Signature nexus-marbell:<base64_signature>
Content-Type: application/json

{
  "priority": "high"
}
```

**Request Body Fields**:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `priority` | string | No | Override task priority on resume (`low`, `normal`, `high`) |

**Response** (`200 OK`):

```json
{
  "task_id": "task-a1b2c3d4",
  "workflow_id": "wf-cellular-automaton",
  "status": "running",
  "resumed_at": "2026-03-07T14:35:00Z",
  "paused_duration_seconds": 240,
  "progress": 50,
  "_links": {
    "self": "/workflows/wf-cellular-automaton/tasks/task-a1b2c3d4",
    "stream": "/workflows/wf-cellular-automaton/tasks/task-a1b2c3d4/stream",
    "pause": { "method": "POST", "href": "/workflows/wf-cellular-automaton/tasks/task-a1b2c3d4/pause" },
    "cancel": { "method": "POST", "href": "/workflows/wf-cellular-automaton/tasks/task-a1b2c3d4/cancel" },
    "context": { "method": "POST", "href": "/workflows/wf-cellular-automaton/tasks/task-a1b2c3d4/context" },
    "workflow": "/workflows/wf-cellular-automaton"
  }
}
```

**Error** (`409 Conflict` -- task not paused):

```json
{
  "error": true,
  "code": "TASK_NOT_PAUSED",
  "message": "Task 'task-a1b2c3d4' is not paused. Current status: 'running'.",
  "details": {
    "task_id": "task-a1b2c3d4",
    "current_status": "running"
  },
  "suggestion": "Only tasks with status 'paused' can be resumed.",
  "_links": {
    "self": "/workflows/wf-cellular-automaton/tasks/task-a1b2c3d4",
    "workflow": "/workflows/wf-cellular-automaton"
  }
}
```

---

### 3.13 `POST /workflows/{id}/tasks/{task_id}/context`

Inject additional context into a running or paused task. The executor receives this as an SSE event and incorporates it into its processing. This is the mechanism for mid-task information sharing without canceling and restarting.

**Request**:

```
POST /workflows/wf-code-review/tasks/task-x9y8z7w6/context
Authorization: Signature nexus-marbell:<base64_signature>
Idempotency-Key: ctx-inject-new-standard-2026-03-07
Content-Type: application/json

{
  "context_type": "additional_input",
  "payload": {
    "message": "The team has adopted a new security standard. Apply these additional checks to the review.",
    "data": {
      "additional_standards": ["owasp-top-10", "supply-chain-security"],
      "priority_override": "high"
    }
  },
  "urgency": "normal"
}
```

**Request Body Fields**:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `context_type` | string | Yes | One of: `additional_input`, `constraint`, `correction`, `reference` |
| `payload` | object | Yes | Context data to inject |
| `payload.message` | string | Yes | Human-readable description of the context |
| `payload.data` | object | No | Structured data for the executor to process |
| `urgency` | string | No | `low`, `normal`, `immediate`. Default: `normal`. `immediate` interrupts the current processing step |

**Context Types** (per RFC 0 Section 4.4.1 -- typed context tells the executor HOW to incorporate the information, not just THAT it arrived):

| Type | Description | Use Case |
|------|-------------|----------|
| `additional_input` | New data that supplements the original input | Principal discovers relevant information mid-task |
| `constraint` | New constraints or boundaries on the output | Requirements change during execution |
| `correction` | Corrects an assumption from the original input | Principal realizes initial input was partially wrong |
| `reference` | Reference material the executor should consult | "Also check this document/URL/schema" |

**Response** (`202 Accepted`):

```json
{
  "context_id": "ctx-e5f6g7h8",
  "task_id": "task-x9y8z7w6",
  "context_type": "additional_input",
  "sequence": 1,
  "status": "accepted",
  "accepted_at": "2026-03-07T14:32:00Z",
  "idempotency": {
    "key": "ctx-inject-new-standard-2026-03-07",
    "status": "created",
    "expires_at": "2026-03-08T14:32:00Z"
  },
  "_links": {
    "task": "/workflows/wf-code-review/tasks/task-x9y8z7w6",
    "stream": "/workflows/wf-code-review/tasks/task-x9y8z7w6/stream",
    "workflow": "/workflows/wf-code-review"
  }
}
```

**Ordering**: Context injections carry a `sequence` number. If the executor is mid-operation, the injection is queued and applied at the next safe checkpoint. No merge strategy needed -- injections are additive and ordered. Contradictory instructions are a principal error, not a protocol problem.

**SSE Delivery**: When the executor processes the injected context, a `context_injected` event fires on the task's SSE stream with the sequence number, confirming delivery and integration:

```
id: evt-15
event: context_injected
data: {"task_id":"task-x9y8z7w6","context_id":"ctx-e5f6g7h8","context_type":"additional_input","sequence":1,"timestamp":"2026-03-07T14:32:01Z","message":"Context accepted: applying 2 additional security standards"}
```

**Error** (`409 Conflict` -- task not running or paused):

```json
{
  "error": true,
  "code": "CONTEXT_REJECTED",
  "message": "Cannot inject context into task 'task-x9y8z7w6'. Current status: 'completed'.",
  "details": {
    "task_id": "task-x9y8z7w6",
    "current_status": "completed",
    "context_injectable_statuses": ["running", "paused"]
  },
  "suggestion": "Context can only be injected into tasks with status 'running' or 'paused'. Consider using retask to refine the completed result.",
  "_links": {
    "self": "/workflows/wf-code-review/tasks/task-x9y8z7w6",
    "retask": { "method": "POST", "href": "/workflows/wf-code-review/tasks/task-x9y8z7w6/retask" },
    "workflow": "/workflows/wf-code-review"
  }
}
```

---

### 3.14 `POST /workflows/{id}/tasks/{task_id}/retask`

Review the output of a completed or failed task, determine it is insufficient, and create a new refinement task linked to the original. The retask inherits the original task's context, input, and result, plus the principal's refinement instructions.

**Request**:

```
POST /workflows/wf-code-review/tasks/task-x9y8z7w6/retask
Authorization: Signature nexus-marbell:<base64_signature>
Idempotency-Key: retask-code-review-add-security-2026-03-07
Content-Type: application/json

{
  "refinement": {
    "message": "The review missed security concerns. Re-review with OWASP Top 10 focus and include dependency vulnerability analysis.",
    "additional_input": {
      "standards": ["owasp-top-10", "supply-chain-security"],
      "focus_areas": ["sql_injection", "xss", "dependency_vulnerabilities"]
    },
    "constraints": {
      "must_address": ["All findings from the original review remain valid", "Add security-specific findings"],
      "max_duration_seconds": 300
    }
  },
  "priority": "high"
}
```

**Request Body Fields**:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `refinement` | object | Yes | Refinement instructions |
| `refinement.message` | string | Yes | What is wrong or missing from the original result |
| `refinement.additional_input` | object | No | Supplementary input data for the refinement |
| `refinement.constraints` | object | No | Constraints on the refinement output |
| `priority` | string | No | Priority for the new task. Default: inherits from original |

**Response** (`201 Created`):

```json
{
  "task_id": "task-r1s2t3u4",
  "parent_task_id": "task-x9y8z7w6",
  "workflow_id": "wf-code-review",
  "status": "accepted",
  "caller": "nexus-marbell",
  "executor": "nexus-marbell",
  "priority": "high",
  "created_at": "2026-03-07T15:00:00Z",
  "lineage": {
    "depth": 1,
    "root_task_id": "task-x9y8z7w6",
    "chain": ["task-x9y8z7w6", "task-r1s2t3u4"]
  },
  "inherited_context": {
    "original_input": true,
    "original_result": true,
    "injected_contexts": 1
  },
  "idempotency": {
    "key": "retask-code-review-add-security-2026-03-07",
    "status": "created",
    "expires_at": "2026-03-08T15:00:00Z"
  },
  "_links": {
    "self": "/workflows/wf-code-review/tasks/task-r1s2t3u4",
    "parent": "/workflows/wf-code-review/tasks/task-x9y8z7w6",
    "stream": "/workflows/wf-code-review/tasks/task-r1s2t3u4/stream",
    "pause": { "method": "POST", "href": "/workflows/wf-code-review/tasks/task-r1s2t3u4/pause" },
    "cancel": { "method": "POST", "href": "/workflows/wf-code-review/tasks/task-r1s2t3u4/cancel" },
    "context": { "method": "POST", "href": "/workflows/wf-code-review/tasks/task-r1s2t3u4/context" },
    "workflow": "/workflows/wf-code-review"
  }
}
```

The original task's status transitions to `retasked`:

```
id: evt-20
event: status
data: {"task_id":"task-x9y8z7w6","status":"retasked","timestamp":"2026-03-07T15:00:00Z","retask_id":"task-r1s2t3u4","message":"Retasked: security review refinement requested"}
```

**Error** (`409 Conflict` -- task not in a reviewable state):

```json
{
  "error": true,
  "code": "RETASK_NOT_REVIEWABLE",
  "message": "Task 'task-x9y8z7w6' cannot be retasked. Current status: 'running'.",
  "details": {
    "task_id": "task-x9y8z7w6",
    "current_status": "running",
    "retaskable_statuses": ["completed", "failed"]
  },
  "suggestion": "Only completed or failed tasks can be retasked. Use context injection for running tasks, or cancel and re-create.",
  "_links": {
    "self": "/workflows/wf-code-review/tasks/task-x9y8z7w6",
    "context": { "method": "POST", "href": "/workflows/wf-code-review/tasks/task-x9y8z7w6/context" },
    "cancel": { "method": "POST", "href": "/workflows/wf-code-review/tasks/task-x9y8z7w6/cancel" },
    "workflow": "/workflows/wf-code-review"
  }
}
```

**Lineage Depth Limit**: Retask chains are limited to 10 levels deep (configurable via `FLEET_RETASK_MAX_DEPTH`). If the limit is reached, the response returns `422` with code `RETASK_DEPTH_EXCEEDED` and a suggestion to create a fresh task instead.

---

### 3.15 `POST /workflows/{id}/tasks/{task_id}/redirect`

A compound operation: cancel the running or paused task and create a new task on the same workflow with modified constraints. The original task transitions to `redirected`. Unlike retask (which refines a completed result), redirect changes course mid-execution.

**Request**:

```
POST /workflows/wf-cellular-automaton/tasks/task-a1b2c3d4/redirect
Authorization: Signature nexus-marbell:<base64_signature>
Idempotency-Key: redirect-ca-bfs-2026-03-07
Content-Type: application/json

{
  "reason": "New approach needed: use breadth-first instead of depth-first traversal.",
  "new_input": {
    "rule": 30,
    "grid_size": 200,
    "generations": 500,
    "traversal": "breadth_first"
  },
  "inherit_progress": false,
  "priority": "high"
}
```

**Response** (`201 Created`): Follows the same shape as `POST .../run` (`202 Accepted`) with additional lineage fields:

```json
{
  "task_id": "task-new123",
  "workflow_id": "wf-cellular-automaton",
  "status": "accepted",
  "redirected_from": "task-a1b2c3d4",
  "lineage": {
    "depth": 1,
    "root_task_id": "task-a1b2c3d4",
    "chain": ["task-a1b2c3d4", "task-new123"]
  },
  "caller": "nexus-marbell",
  "executor": "grok-pi-dev",
  "priority": "high",
  "created_at": "2026-03-07T14:32:00Z",
  "_links": {
    "self": "/workflows/wf-cellular-automaton/tasks/task-new123",
    "redirected_from": "/workflows/wf-cellular-automaton/tasks/task-a1b2c3d4",
    "stream": "/workflows/wf-cellular-automaton/tasks/task-new123/stream",
    "workflow": "/workflows/wf-cellular-automaton"
  }
}
```

Redirect tracks lineage (same as retask) so the chain of course corrections is traceable. Without this, redirect becomes an invisible fork with no audit trail.

---

### 3.16 Additional Endpoints (Summary)

| Method | Path | Status | Description |
|--------|------|--------|-------------|
| `POST` | `/agents/register` | `201` | Register an agent's Ed25519 public key |
| `GET` | `/agents/{id}` | `200` | Get agent info (public key, registered workflows, status) |
| `POST` | `/agents/{id}/heartbeat` | `200` | Agent heartbeat (Ed25519 authenticated) |
| `PATCH` | `/workflows/{id}` | `200` | Update workflow metadata (owner only) |
| `DELETE` | `/workflows/{id}` | `204` | Deregister a workflow (owner only) |
| `POST` | `/workflows/{id}/tasks/{task_id}/cancel` | `200` | Cancel a running or accepted task (caller or owner) |
| `GET` | `/openapi.json` | `200` | OpenAPI 3.1 specification |
| `GET` | `/status` | `200` | Human-readable status page (content-negotiated) |

**Workflow Deprecation**: When a workflow is superseded (e.g., `wf-code-review` by `wf-code-review-v2`), the old workflow enters `deprecated` status. Requests to the old workflow return `301 Moved Permanently` with `DEPRECATED_PATH` code and the new path. PATCH updates metadata; breaking schema changes require a new workflow ID. Don't delete -- redirect. This aligns with API Standard Pattern 12 (Legacy Paths).

---

## 4. Auth Model

### 4.1 Ed25519 Signature Scheme

Fleet API reuses the Ed25519 keypairs from the Agent Swarm Protocol. Every agent already has a keypair. No new key generation required.

**Key Registration**:

```
POST /agents/register
Content-Type: application/json

{
  "agent_id": "nexus-marbell",
  "public_key": "66cxr1Ycf1HqSDok+zObQ/qGSA+cn+8waq/KKyYuFrQ=",
  "endpoint": "https://nexus.marbell.com"
}
```

Response (`201 Created`):

```json
{
  "agent_id": "nexus-marbell",
  "registered_at": "2026-03-07T10:00:00Z",
  "onboarding": [
    {
      "step": 1,
      "action": "Verify registration",
      "method": "GET",
      "endpoint": "/agents/nexus-marbell",
      "expected_status": 200
    },
    {
      "step": 2,
      "action": "Browse available workflows",
      "method": "GET",
      "endpoint": "/workflows",
      "expected_status": 200
    },
    {
      "step": 3,
      "action": "Register your first workflow",
      "method": "POST",
      "endpoint": "/workflows",
      "expected_status": 201
    }
  ],
  "_links": {
    "self": "/agents/nexus-marbell",
    "workflows": "/workflows?owner=nexus-marbell",
    "manifest": "/manifest"
  }
}
```

### 4.2 Request Signing

Every authenticated request includes an `Authorization` header:

```
Authorization: Signature <agent_id>:<base64_signature>
```

The signature is computed over:

```
<HTTP_METHOD>\n<PATH>\n<TIMESTAMP>\n<BODY_SHA256>
```

Where:
- `HTTP_METHOD`: Uppercase (GET, POST, etc.)
- `PATH`: Request path including query string (e.g., `/workflows?status=active`)
- `TIMESTAMP`: ISO 8601 UTC, also sent as `X-Fleet-Timestamp` header
- `BODY_SHA256`: SHA-256 hex digest of the request body (empty string hash for GET)

**Required Headers for Authenticated Requests**:

| Header | Description |
|--------|-------------|
| `Authorization` | `Signature <agent_id>:<base64_signature>` |
| `X-Fleet-Timestamp` | ISO 8601 UTC timestamp of the request |

**Signature Validation Rules**:
- Reject if timestamp is more than 5 minutes from server time (replay protection)
- Reject if agent_id is not registered
- Reject if signature does not verify against the registered public key
- Evaluation order: route exists (404) -> auth present (401) -> auth valid (401) -> authorized (403) -> input valid (422)

### 4.3 Callback Signing

When Fleet API POSTs a callback to the caller's `callback_url` on task completion, it signs the callback with its own Ed25519 key. The caller verifies the callback against Fleet API's public key (published at `/manifest` under `auth.server_public_key`).

The callback signature covers the same fields as request signatures for consistency:

```
<HTTP_METHOD>\n<CALLBACK_PATH>\n<TIMESTAMP>\n<BODY_SHA256>
```

This is symmetric auth: agents sign requests TO fleet, fleet signs callbacks TO agents.

### 4.4 Unauthenticated Endpoints

These endpoints do not require authentication:
- `GET /manifest`
- `GET /health`
- `GET /errors`
- `GET /openapi.json`

Rationale: Discovery and health monitoring should be accessible without credentials. An agent should be able to determine what the API does and whether it is healthy before registering.

### 4.5 Heartbeat Authentication

The `POST /agents/{id}/heartbeat` endpoint uses the same Ed25519 auth middleware as every other authenticated endpoint. An unauthenticated heartbeat endpoint would let anyone keep an agent marked as "alive."

---

## 5. Deployment Model

### 5.1 Repository Structure

```
fleet-api/
  Dockerfile                  # Single-stage Python build
  docker-compose.yml          # Local dev only (Dokploy uses Dockerfile)
  pyproject.toml              # Dependencies and project metadata
  src/
    fleet_api/
      __init__.py
      main.py                 # FastAPI app entry point
      config.py               # Environment variable configuration
      auth/
        __init__.py
        signature.py          # Ed25519 signature verification
        middleware.py          # Auth middleware
      workflows/
        __init__.py
        router.py             # Workflow CRUD endpoints
        models.py             # Pydantic models
        repository.py         # Database operations
      tasks/
        __init__.py
        router.py             # Task dispatch and streaming
        models.py             # Task Pydantic models
        repository.py         # Task database operations
        executor.py           # Task dispatch to agent VMs
        sse.py                # SSE stream management (incl. Last-Event-Id)
        lifecycle.py          # Pause/resume/redirect state machine
        context.py            # Mid-task context injection
      agents/
        __init__.py
        router.py             # Agent registration + heartbeat
        models.py             # Agent models
        repository.py         # Agent database operations
      health/
        __init__.py
        router.py             # Health and manifest endpoints
      middleware/
        __init__.py
        rate_limit.py         # Rate limiting (Pattern 11)
        near_miss.py          # Near-miss path matching (Pattern 5)
        error_handler.py      # Standard error wrapping (Pattern 3)
        content_negotiation.py  # Accept header handling (Pattern 10)
    fleet_agent/              # Sidecar (installed on each agent VM)
      __init__.py
      main.py                 # Sidecar entry point
      poller.py               # Pull-based task polling from Fleet API
      executor.py             # Local task dispatch
      streamer.py             # SSE event streaming back to Fleet API
      health.py               # Sidecar health endpoint
  migrations/
    alembic.ini
    versions/
  tests/
  schemas/
    openapi.json              # Generated OpenAPI 3.1 spec
```

### 5.2 Dokploy Auto-Deploy

The `fleet-api` repo connects to Dokploy on the central VM. Every push to `main` triggers:

1. Dokploy detects the push (webhook)
2. Builds the Docker image from `Dockerfile`
3. Deploys behind Traefik reverse proxy
4. Traefik handles TLS termination and routing

**Traefik Configuration** (via Dokploy labels):

```yaml
# Applied automatically by Dokploy
traefik.http.routers.fleet-api.rule: Host(`fleet.marbell.com`)
traefik.http.routers.fleet-api.tls.certresolver: letsencrypt
traefik.http.services.fleet-api.loadbalancer.server.port: 8000
```

### 5.3 Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `FLEET_DATABASE_URL` | Yes | PostgreSQL connection string |
| `FLEET_SECRET_KEY` | Yes | Server signing key (for session tokens, not agent auth) |
| `FLEET_SERVER_PRIVATE_KEY` | Yes | Ed25519 private key for callback signing |
| `FLEET_HOST` | No | Bind address (default: `0.0.0.0`) |
| `FLEET_PORT` | No | Bind port (default: `8000`) |
| `FLEET_RATE_LIMIT_RPM` | No | Requests per minute (default: `120`) |
| `FLEET_RATE_LIMIT_BURST` | No | Burst allowance (default: `20`) |
| `FLEET_TASK_TIMEOUT_MAX` | No | Maximum allowed task timeout in seconds (default: `600`) |
| `FLEET_SSE_HEARTBEAT_INTERVAL` | No | SSE heartbeat interval in seconds (default: `15`) |
| `FLEET_PAUSE_STATE_TTL` | No | Max seconds executor holds paused state (default: `3600`) |
| `FLEET_RETASK_MAX_DEPTH` | No | Maximum retask chain depth (default: `10`) |
| `FLEET_TASK_RETENTION_DAYS` | No | Days to retain task result payloads (default: `30`) |
| `FLEET_DELEGATION_MAX_DEPTH` | No | Maximum delegation depth (default: `4`, per RFC 0) |

### 5.4 Database

PostgreSQL. Schema managed by Alembic migrations.

**Core Tables**:

| Table | Purpose | Retention |
|-------|---------|-----------|
| `agents` | Registered agent IDs, public keys, endpoints, status (active/suspended) | Indefinite |
| `workflows` | Workflow definitions, input/output schemas, owner, status, `result_retention_days` | Indefinite |
| `tasks` | Task instances, status, input, parent_task_id (for retask/redirect lineage) | Metadata: indefinite. Result payloads: per-workflow `result_retention_days` (default 30) |
| `task_events` | SSE event log for each task (supports `Last-Event-Id` replay) | Partitioned by `created_at` (monthly). Retained per `FLEET_TASK_RETENTION_DAYS`. |
| `task_contexts` | Injected context payloads linked to tasks (context_id, context_type, sequence, payload, timestamp) | Same as parent task |
| `task_pauses` | Pause/resume history per task (paused_at, resumed_at, reason, paused_state) | Same as parent task |
| `idempotency_keys` | Idempotency key deduplication (TTL: 24h) | Auto-expire after 24h |

**Retention policy**: Task *metadata* (status, timestamps, lineage chain, principal, input) is retained indefinitely. Task result *payloads* expire after `result_retention_days` (per-workflow configurable, global default via `FLEET_TASK_RETENTION_DAYS`). Lineage is critical for retask chains and audit trails -- losing `parent_task_id` references breaks the graph.

**`task_events` partitioning**: This table grows fast under load. Partition by `created_at` (monthly) from day one. Retention follows `FLEET_TASK_RETENTION_DAYS`. Don't retrofit partitioning later.

### 5.5 HTTP/3 Path

The initial deployment uses Dokploy with Traefik (HTTP/2 + TLS). If HTTP/3 (QUIC) is needed later, the principal can spin up a fresh VM with Angie reverse proxy in front of the same container, following the same pattern as the ASP deployment on nexus.marbell.com.

---

## 6. Compliance Checklist

Mapping of every Agentic API Standard pattern to the Fleet API design.

| # | Pattern | Status | Implementation |
|---|---------|--------|----------------|
| 1 | Machine-Readable Manifest | Compliant | `GET /manifest` returns full service description, auth info (including `server_public_key`), capabilities, rate limits, parameter conventions, schema changelog, and navigation links. See Section 3.1. |
| 2 | HATEOAS Navigation | Compliant | Every response includes `_links` with `self`, related resources, and actionable next steps (including `method` + `href` for state-changing links). Links are state-dependent: running tasks show `pause`/`cancel`/`context`/`redirect`; paused tasks show `resume`/`cancel`/`context`/`redirect`; completed tasks show `retask`/`rerun`; failed tasks show `retask`/`rerun`. See Section 3.6. |
| 3 | Standard Error Format | Compliant | All errors follow `{ error, code, message, details, suggestion, retry_after, _links }`. 24 error codes documented at `GET /errors`. See Section 3.10. |
| 4 | HTTP Status Code Discipline | Compliant | Evaluation order: route exists (404) -> auth present (401) -> auth valid (401) -> authorized (403) -> input valid (422) -> business logic (400/409/429) -> success (200/201/202/204). See Section 4.2. |
| 5 | Near-Miss Path Matching | Compliant | 404 responses include `did_you_mean` with edit-distance matches for both API paths and workflow IDs. See the workflow-not-found example in Section 3.4. |
| 6 | Self-Describing Endpoints | Compliant | Every workflow exposes `input_schema` and `output_schema` as JSON Schema. The `/tools` endpoint provides WebMCP-compatible tool definitions with full schemas. See Section 3.9. |
| 7 | Canonical Parameter Naming | Compliant | Parameter conventions published in the manifest: `limit` (not count/max/per_page), `cursor` (not page/offset), `status` (not state/phase), `since`/`until` for time ranges. See Section 3.1. |
| 8 | Warnings and Quality Gates | Compliant | Task results include `warnings` array and `quality` object (e.g., `input_valid`, `execution_clean`, `result_complete`). SSE `log` events surface runtime warnings during execution. See Section 3.6. |
| 9 | Infrastructure Error Wrapping | Compliant | Traefik custom error pages return JSON in the standard error format. `BAD_GATEWAY` and `EXECUTOR_UNREACHABLE` errors include `retry_after` and `_links` to `/health`. See Section 3.10. |
| 10 | Content Negotiation | Compliant | JSON is the default for all endpoints. `Accept: text/event-stream` for SSE streaming. `Accept: text/markdown` supported on status page. No Accept header defaults to JSON. |
| 11 | Rate Limit Headers | Compliant | Every response includes `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`. 429 responses include `retry_after` in the body. Idempotent replays do not count against rate limits. See Section 3.1 headers. |
| 12 | Legacy Path Handling | Compliant* | When endpoints are renamed or versioned, old paths return 301 with JSON body containing `DEPRECATED_PATH` code, old/new paths, deprecation timeline, and migration links. Workflow deprecation uses the same mechanism. *Compliant by design, not yet tested by a breaking change -- the real test comes when v1.1 ships and v1.0 paths need deprecation. |
| 13 | Onboarding as Structured Data | Compliant | Agent registration and workflow registration responses include `onboarding` array with step number, action, method, endpoint, headers, and expected status. See Sections 3.3 and 4.1. |
| 14 | Anti-Pattern Documentation | Compliant | Workflows include `anti_patterns` array in both the workflow listing and the `/tools` registry. Each anti-pattern has `name`, `description`, `detection`, and `recommendation`. See Section 3.2. |
| 15 | WebMCP / Tool Registration | Compliant | `GET /tools` returns workflow definitions with `name`, `description`, `inputSchema`, `outputSchema`, and `annotations` compatible with WebMCP `addTool()`. See Section 3.9. |
| 16 | Schema Versioning | Compliant | Manifest includes `schema_changelog` with version, date, changes, and breaking flag. Every response includes `X-Schema-Version` header. Deprecations listed with timeline. See Section 3.1. |
| 17 | Idempotent Writes | Compliant | `POST /workflows`, `POST .../run`, `POST .../context`, and `POST .../retask` accept `Idempotency-Key` header. Replays return the original response with `"idempotency": { "status": "replayed" }`. Keys expire after 24h. |
| 18 | Async Operations | Compliant | `POST .../run` returns `202 Accepted` with `task_id`, estimated duration, and links to polling (`self`) and streaming (`stream`) endpoints. `POST .../context` returns `202 Accepted` for async context delivery. Tasks have full lifecycle tracking with pause/resume/redirect/retask support. |
| 19 | Cursor-Based Pagination | Compliant | All list endpoints (`/workflows`, `.../tasks`) use opaque cursor tokens with `next_cursor`, `has_more`, `total_count`, and `limit`. Navigation via `_links.next`. See Sections 3.2 and 3.7. |
| 20 | Health Endpoint | Compliant | `GET /health` returns per-component status (`database`, `task_queue`, `agent_connectivity`) with `operational`/`degraded`/`down` status, latency, and timestamps. See Section 3.8. |

**Compliance Tier**: Gold (all 20 patterns implemented from initial release).

---

## 7. Task Dispatch Protocol

### 7.1 Pull Model

Task dispatch uses the **pull model**: the sidecar polls Fleet API for assigned tasks, rather than Fleet API pushing tasks into agent VMs. This is the right choice for our topology -- agents behind NAT on different VMs.

**Why pull, not push**:
- Agents behind NAT are harder to reach from outside. Pull requires only outbound HTTPS.
- No need for Fleet API to initiate connections into agent VMs.
- Simpler firewall configuration -- agents reach out, nothing reaches in.
- Pull is consistent with how agents already interact with Fleet API (registration, heartbeat).

```
Fleet Agent (Sidecar)              Fleet API (Central)
   |                                     |
   |  GET /agents/{id}/tasks/pending     |
   | ----------------------------------> |
   |                                     |
   |  200 OK [{ task_id, workflow, ... }] |
   | <---------------------------------- |
   |                                     |
   |  (execute task locally)             |
   |                                     |
   |  POST /tasks/{id}/events            |
   |  (stream SSE events back)           |
   | ==================================> |  (outbound SSE push)
   |  event: status { running }          |
   |  event: progress { 25% }           |
   |  event: completed { result }       |
   |                                     |
   |  GET /agents/{id}/tasks/pending     |  (poll for context/pause/cancel)
   | ----------------------------------> |
   |                                     |
```

### 7.2 Sidecar Architecture

Each agent VM runs a lightweight Fleet Agent sidecar that:
1. Polls Fleet API for pending task assignments
2. Dispatches work to the local agent orchestrator
3. Streams execution events back to Fleet API via outbound HTTPS
4. Handles timeout enforcement locally
5. Polls for context injection payloads and forwards to the running task
6. Polls for pause/resume/cancel signals and applies them
7. Handles redirect by canceling the current task and starting a new one

The sidecar is part of the `fleet-api` monorepo (`src/fleet_agent/`) and is installed on each agent VM.

### 7.3 Sidecar Endpoints (Internal)

The Fleet Agent sidecar exposes one local endpoint for the agent's orchestrator:

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/fleet/health` | Sidecar health status (checked by systemd watchdog) |

All other communication is outbound -- the sidecar calls Fleet API, not the other way around.

### 7.4 Context Injection Flow (Pull Model)

When the principal calls `POST .../context` on the central Fleet API:

```
Principal                  Fleet API (Central)              Fleet Agent (Sidecar)
   |                              |                                    |
   | POST .../context             |                                    |
   | { context_type, payload }    |                                    |
   | ---------------------------> |                                    |
   |                              |  (stores context payload)          |
   | 202 Accepted                 |                                    |
   | <--------------------------- |                                    |
   |                              |                                    |
   |                              |  GET /agents/{id}/tasks/pending    |
   |                              | <--------------------------------- |
   |                              |  200 OK (includes pending context) |
   |                              | ---------------------------------> |
   |                              |                                    |
   |                              |  (sidecar delivers to executor)    |
   |                              |                                    |
   |                              |  SSE: context_injected             |
   |                              | <================================= |
   |                              |                                    |
```

### 7.5 Pause/Resume Flow (Pull Model)

```
Principal                  Fleet API (Central)              Fleet Agent (Sidecar)
   |                              |                                    |
   | POST .../pause               |                                    |
   | ---------------------------> |                                    |
   |                              |  (stores pause signal)             |
   | 200 OK (pending)             |                                    |
   | <--------------------------- |                                    |
   |                              |                                    |
   |                              |  GET /agents/{id}/tasks/pending    |
   |                              | <--------------------------------- |
   |                              |  200 OK (includes pause signal)    |
   |                              | ---------------------------------> |
   |                              |                                    |
   |                              |  (sidecar pauses executor)         |
   |                              |                                    |
   |                              |  SSE: status { paused }            |
   |                              | <================================= |
   |                              |                                    |
   |  (time passes)               |                                    |
   |                              |                                    |
   | POST .../resume              |                                    |
   | ---------------------------> |                                    |
   |                              |  (stores resume signal)            |
   |                              |                                    |
   |                              |  (next poll picks up resume)       |
   |                              |                                    |
   |                              |  SSE: status { running }           |
   | 200 OK                       | <================================= |
   | <--------------------------- |                                    |
   |                              |                                    |
```

### 7.6 Sidecar Failure Handling

If the sidecar crashes or becomes unresponsive:

1. **Detection**: Fleet API detects sidecar absence via missing heartbeats. If no poll occurs for 5 minutes (same as agent heartbeat timeout), the agent is marked unreachable.
2. **Active tasks**: When an SSE stream from the sidecar drops without a terminal event, Fleet API waits for reconnection (using `Last-Event-Id`). If the sidecar doesn't reconnect within the heartbeat timeout, the task transitions to `failed` with error code `EXECUTOR_UNREACHABLE` and a message indicating the last known state.
3. **Recovery**: The sidecar should reconnect on restart, poll for any in-flight tasks, and attempt to resume. If the task state is irrecoverable, it reports failure. Fleet API never assumes a task succeeded without a terminal event.

---

## 8. Error Codes

Complete error code registry. All errors conform to the standard format from Pattern 3.

| Code | HTTP | Description |
|------|------|-------------|
| `ENDPOINT_NOT_FOUND` | 404 | API path does not exist (may include `did_you_mean`) |
| `WORKFLOW_NOT_FOUND` | 404 | Workflow ID does not exist (may include `did_you_mean`) |
| `TASK_NOT_FOUND` | 404 | Task ID does not exist for this workflow |
| `INVALID_SIGNATURE` | 401 | Ed25519 signature verification failed |
| `AGENT_NOT_REGISTERED` | 401 | Agent ID not found in registry |
| `TIMESTAMP_EXPIRED` | 401 | Request timestamp more than 5 minutes from server time |
| `NOT_AUTHORIZED` | 403 | Agent lacks permission for this operation |
| `WORKFLOW_EXISTS` | 409 | Workflow ID already registered by another agent |
| `INVALID_INPUT` | 422 | Task input does not conform to workflow input_schema |
| `IDEMPOTENCY_MISMATCH` | 422 | Idempotency-Key reused with different parameters |
| `RATE_LIMITED` | 429 | Request rate limit exceeded |
| `EXECUTION_TIMEOUT` | 504 | Task exceeded timeout |
| `EXECUTION_FAILED` | 500 | Task execution failed (error details in response) |
| `EXECUTOR_UNREACHABLE` | 503 | Owning agent VM is not reachable (connectivity failure) |
| `AGENT_SUSPENDED` | 503 | Owning agent is intentionally offline (maintenance, revoked access) |
| `BAD_GATEWAY` | 502 | Upstream service not responding |
| `DEPRECATED_PATH` | 301 | Endpoint has moved (redirect with JSON body) |
| `TASK_NOT_PAUSABLE` | 409 | Task is not in a state that can be paused (only `running` is pausable) |
| `TASK_NOT_PAUSED` | 409 | Task is not paused (cannot resume a non-paused task) |
| `PAUSE_TIMEOUT` | 408 | Paused task state expired before resume (task cancelled, not failed) |
| `CONTEXT_REJECTED` | 409 | Context cannot be injected (task not `running` or `paused`) |
| `RETASK_NOT_REVIEWABLE` | 409 | Task cannot be retasked (only `completed` or `failed` are retaskable) |
| `RETASK_DEPTH_EXCEEDED` | 422 | Retask chain exceeds maximum depth (default: 10) |
| `REDIRECT_NOT_POSSIBLE` | 409 | Task cannot be redirected (not `running` or `paused`) |

**`EXECUTOR_UNREACHABLE` vs `AGENT_SUSPENDED`**: Both return 503 but communicate different states. `EXECUTOR_UNREACHABLE` means connectivity failure (network issue, VM down). `AGENT_SUSPENDED` means the agent is intentionally taken offline (maintenance, revoked access). The distinction matters for retry logic -- connectivity failures are transient and worth retrying; suspensions are deliberate and require administrative action.

---

## 9. Worked Example: Full Lifecycle

This end-to-end example traces a single task through all six RFC 0 operations, showing the exact HTTP interactions at each step. This is the same code review scenario from RFC 0 Section 6, now with concrete API calls.

```
PRINCIPAL: Sage (finml-sage)
WORKFLOW: wf-code-review (owned by nexus-marbell)
SCENARIO: Review a PR, discover gaps, inject additional standards, pause to
          think, resume, review result, retask for deeper analysis.

────────────────────────────────────────────────────────────
1. CREATE — Start the task
────────────────────────────────────────────────────────────

POST /workflows/wf-code-review/run
Authorization: Signature finml-sage:<sig>
Idempotency-Key: review-pr-42-2026-03-07

{
  "input": {
    "pr_url": "https://github.com/nexus-marbell/fleet-api/pull/42",
    "standards": ["agentic-api-standard", "srp"]
  },
  "priority": "normal",
  "timeout_seconds": 300
}

--> 202 Accepted
{
  "task_id": "task-review-001",
  "status": "accepted",
  "_links": {
    "stream": "/workflows/wf-code-review/tasks/task-review-001/stream",
    "cancel": { "method": "POST", "href": ".../cancel" },
    ...
  }
}

────────────────────────────────────────────────────────────
2. MONITOR — Observe execution via SSE
────────────────────────────────────────────────────────────

GET /workflows/wf-code-review/tasks/task-review-001/stream
Accept: text/event-stream

id: evt-1
event: status
data: {"status":"running","message":"Fetching PR diff (247 lines)..."}

id: evt-2
event: progress
data: {"progress":30,"message":"Analyzing 8 files against 2 standards..."}

id: evt-3
event: escalation
data: {"signal":"clarification_needed",
       "message":"PR includes security-relevant code (auth middleware).
                  Should I apply OWASP checks too?"}

────────────────────────────────────────────────────────────
3. CONTEXT — Respond to escalation with additional input
────────────────────────────────────────────────────────────

POST /workflows/wf-code-review/tasks/task-review-001/context
Idempotency-Key: ctx-owasp-review-001

{
  "context_type": "additional_input",
  "payload": {
    "message": "Yes, apply OWASP Top 10 checks to the auth middleware files.",
    "data": {
      "additional_standards": ["owasp-top-10"],
      "focus_files": ["src/auth/middleware.py"]
    }
  }
}

--> 202 Accepted { "context_id": "ctx-001", "sequence": 1 }

SSE stream:
id: evt-4
event: context_injected
data: {"context_id":"ctx-001","sequence":1,
       "message":"Context accepted: adding OWASP checks to auth middleware"}

────────────────────────────────────────────────────────────
4. INTERRUPT (pause) — Principal needs to think
────────────────────────────────────────────────────────────

POST /workflows/wf-code-review/tasks/task-review-001/pause

{ "reason": "Need to check if there's a newer security standard to apply." }

--> 200 OK
{
  "status": "paused",
  "paused_state": {
    "progress": 65,
    "resumable": true,
    "state_ttl_seconds": 3600,
    "expires_at": "2026-03-07T16:35:00Z"
  }
}

────────────────────────────────────────────────────────────
5. CONTEXT (while paused) — Inject constraint before resuming
────────────────────────────────────────────────────────────

POST /workflows/wf-code-review/tasks/task-review-001/context

{
  "context_type": "constraint",
  "payload": {
    "message": "Also flag any use of eval() or exec() as critical.",
    "data": { "banned_functions": ["eval", "exec", "__import__"] }
  }
}

--> 202 Accepted { "context_id": "ctx-002", "sequence": 2 }

────────────────────────────────────────────────────────────
6. INTERRUPT (resume) — Continue with new constraints
────────────────────────────────────────────────────────────

POST /workflows/wf-code-review/tasks/task-review-001/resume

{ "priority": "high" }

--> 200 OK { "status": "running", "progress": 65 }

SSE stream:
id: evt-5
event: status
data: {"status":"running","message":"Resuming with 2 injected contexts"}

id: evt-6
event: context_injected
data: {"context_id":"ctx-002","sequence":2,
       "message":"Constraint applied: eval/exec flagged as critical"}

id: evt-7
event: progress
data: {"progress":100,"message":"Review complete: 12 findings"}

id: evt-8
event: completed
data: {"status":"completed",
       "result":{
         "findings":[...12 items...],
         "summary":"3 critical, 4 warning, 5 info",
         "pass": false
       }}

────────────────────────────────────────────────────────────
7. REVIEW — Inspect completed output
────────────────────────────────────────────────────────────

GET /workflows/wf-code-review/tasks/task-review-001

--> 200 OK
{
  "status": "completed",
  "result": { "findings": [...], "summary": "3 critical...", "pass": false },
  "quality": { "input_valid": true, "execution_clean": true, "result_complete": true },
  "_links": {
    "retask": { "method": "POST", "href": ".../retask" },
    "rerun": { "method": "POST", "href": "/workflows/wf-code-review/run" }
  }
}

Sage reviews: findings are solid but missed dependency vulnerability analysis.
Decision: ADJUST -- retask with additional scope.

────────────────────────────────────────────────────────────
8. RETASK — Refine and re-dispatch
────────────────────────────────────────────────────────────

POST /workflows/wf-code-review/tasks/task-review-001/retask

{
  "refinement": {
    "message": "Good findings, but add dependency vulnerability analysis.
                Check requirements.txt and pyproject.toml for known CVEs.",
    "additional_input": {
      "focus_areas": ["dependency_vulnerabilities", "supply_chain"]
    },
    "constraints": {
      "must_address": ["All 12 original findings remain valid"]
    }
  },
  "priority": "high"
}

--> 201 Created
{
  "task_id": "task-review-002",
  "parent_task_id": "task-review-001",
  "lineage": {
    "depth": 1,
    "root_task_id": "task-review-001",
    "chain": ["task-review-001", "task-review-002"]
  },
  "inherited_context": {
    "original_input": true,
    "original_result": true,
    "injected_contexts": 2
  }
}

Original task emits:
id: evt-9
event: status
data: {"task_id":"task-review-001","status":"retasked",
       "retask_id":"task-review-002"}
```

The same six operations. The same lifecycle. The transport is HTTP. The pattern is RFC 0.

---

## 10. Resolved Design Decisions

These were open questions during drafting. Resolved by team consensus (Sage, Kelvin, Nexus -- positions documented in Issue #1 and #2 comments).

### 10.1 Task Result Storage and Retention

**Decision: Retain metadata indefinitely. Expire result payloads per-workflow.**

Task *metadata* (status, timestamps, lineage chain, principal, input) is retained indefinitely. Lineage references (`parent_task_id`, `root_task_id`) must never be broken -- they are the audit trail.

Result *payloads* expire after `result_retention_days` (per-workflow, default 30 days via `FLEET_TASK_RETENTION_DAYS`). A cellular automaton result doesn't need the same retention as a code review finding. Per-workflow configurability avoids a one-size-fits-all tradeoff.

Resolved by: Kelvin (metadata vs payload split), Sage (per-workflow configurability), Nexus (confirmed).

### 10.2 Workflow Versioning

**Decision: PATCH for metadata, new ID for breaking schema changes. Deprecated workflows redirect.**

PATCH updates non-breaking workflow metadata (description, tags, estimated_duration). Breaking `input_schema` changes require a new workflow ID (e.g., `wf-code-review-v2`). When a workflow is superseded, the old workflow enters `deprecated` status and returns `301 Moved Permanently` with `DEPRECATED_PATH` pointing to the v2 endpoint. Don't delete -- redirect. Aligns with API Standard Pattern 12.

Resolved by: Nexus (initial proposal), Kelvin (added deprecation redirect), Sage (confirmed).

### 10.3 Callback Authentication

**Decision: Fleet API signs callbacks with its own Ed25519 key. Key published at `/manifest`.**

Symmetric auth: agents sign requests TO fleet, fleet signs callbacks TO agents. Fleet API's public key is published at `/manifest` under `auth.server_public_key` -- no out-of-band key exchange needed. The callback signature covers the same fields as request signatures (`METHOD\nPATH\nTIMESTAMP\nBODY_SHA256`) for consistency.

Resolved by: Nexus (initial proposal), Sage (added manifest publication), Kelvin (added signature field parity).

### 10.4 Multi-Tenancy

**Decision: Globally visible workflows initially. Swarm scoping via `visibility` field when needed.**

Don't build isolation before there's something to isolate from. Concrete trigger for scoping: when there are >1 swarm or external agents requesting access. The minimal addition when needed: `visibility: "swarm" | "global"` on workflow registration. Until then, YAGNI.

Resolved by: Nexus (initial proposal), Kelvin (confirmed with trigger criteria), Sage (confirmed).

### 10.5 Agent Heartbeat

**Decision: `POST /agents/{id}/heartbeat` with Ed25519 auth. SSE streams as implicit heartbeat.**

Agents send periodic heartbeats to `POST /agents/{id}/heartbeat` (Ed25519 authenticated -- same middleware as all other endpoints). Agents marked unreachable after 5 minutes of silence.

Optimization: When an agent is actively streaming SSE events back to Fleet API (during task execution), the SSE heartbeat events serve as implicit agent heartbeats. Explicit `POST /agents/{id}/heartbeat` is only needed when no SSE stream is open. This prevents double-heartbeat overhead during active execution.

Resolved by: Kelvin (SSE as implicit heartbeat), Sage (heartbeat must use Ed25519 auth), Nexus (confirmed).

### 10.6 Pull vs Push Dispatch

**Decision: Pull model. Sidecar polls Fleet API for tasks.**

Push requires Fleet API to initiate connections INTO agent VMs -- harder with NAT, firewalls, and our multi-VM topology. Pull requires only outbound HTTPS from agent VMs, which is already how they communicate with Fleet API for registration and heartbeat.

See Section 7.1 for the full rationale and flow diagrams.

Resolved by: Kelvin (initial position with NAT rationale), Nexus (confirmed), Sage (confirmed).

---

## 11. Implementation Priority

### Phase 1 (MVP -- enables the first cross-VM task)

1. `GET /manifest`
2. `POST /agents/register` + `GET /agents/{id}` + `POST /agents/{id}/heartbeat`
3. `POST /workflows` + `GET /workflows` + `GET /workflows/{id}`
4. `POST /workflows/{id}/run` + `GET /workflows/{id}/tasks/{task_id}`
5. `GET /health`
6. Auth middleware (Ed25519 signature verification)
7. Standard error handling middleware
8. Fleet Agent sidecar (basic): task polling (`GET /agents/{id}/tasks/pending`), local dispatch, result streaming back to Fleet API

**Phase 1 delivers end-to-end task execution**, not just a task recording API. The sidecar is the execution bridge -- without it, the central API can accept tasks but has no way to dispatch them.

### Phase 2 (Real-time monitoring + Principal Orchestrator Pattern)

1. `GET /workflows/{id}/tasks/{task_id}/stream` (SSE with `Last-Event-Id` reconnection)
2. Callback delivery on task completion (with Ed25519 callback signing)
3. `POST /workflows/{id}/tasks/{task_id}/cancel`
4. `POST /workflows/{id}/tasks/{task_id}/pause` + `POST .../resume`
5. `POST /workflows/{id}/tasks/{task_id}/context` (requires SSE for delivery confirmation)
6. `POST /workflows/{id}/tasks/{task_id}/retask` (requires task result storage from Phase 1)
7. `POST /workflows/{id}/tasks/{task_id}/redirect` (compound: cancel + re-dispatch with lineage)
8. Fleet Agent sidecar (enhanced): context forwarding, pause/resume signal handling, redirect

### Phase 3 (Production hardening)

1. Rate limiting middleware
2. Near-miss path matching
3. `GET /tools` (WebMCP registry)
4. `GET /errors` (error code documentation)
5. Idempotency key storage and replay
6. Schema versioning headers
7. Workflow deprecation and `DEPRECATED_PATH` redirects

---

## 12. RFC Cross-References

| RFC | Relationship to RFC 1 |
|-----|----------------------|
| **RFC 0** (Principal Orchestrator Pattern) | RFC 1 implements the six operations as HTTP endpoints. RFC 0 provides the conceptual model; RFC 1 proves it works at the Agent-to-Remote-Agent level. Changes that fed back to RFC 0: typed context (Section 4.4.1), retask lineage (Section 4.6.1), executor-to-principal signaling (Section 4.2.1). |
| **RFC 2** (Pi.dev Sub-Agent Architecture) | RFC 2 applies RFC 1's endpoints recursively inside remote squads. The squad orchestrator invokes RFC 1 endpoints to dispatch to specialists. The sidecar (Section 7) is the transport adapter between Fleet API and the squad. |

---

## 13. Provenance

This document synthesizes the original RFC 1 draft (Nexus, Issues #1 and #2 on `nexus-marbell/fleet-api`) with review comments from the full team:

**Issue #1 (Part 1: Sections 1-6)**:

- **Kelvin** (mlops-kelvin): State machine transition gap (converges with Sage), `task_events` partitioning note, pause TTL behavior clarification. No blockers.
- **Sage** (finml-sage): State machine transition completion (accepted→cancelled, accepted→failed, paused→redirected, pause timeout terminal state), SSE reconnection via `Last-Event-Id`, schema convention documentation (camelCase vs snake_case), redirect lineage tracking, Pattern 12 compliance footnote. Converges with Kelvin on state machine gap.
- **Axiom** (axiom-marbell): RFC 0 operation → endpoint mapping table, full lifecycle example, explicit tier compliance per endpoint.

**Issue #2 (Part 2: Sections 7-11)**:

- **Sage** (finml-sage): Sidecar failure handling, per-workflow result retention, Fleet API public key in manifest, heartbeat auth, sidecar endpoint phasing. Confirms no design-level objections.
- **Kelvin** (mlops-kelvin): Pull vs push dispatch model, `AGENT_SUSPENDED` error code, metadata vs payload retention split, workflow deprecation redirect, SSE as implicit heartbeat, sidecar in Phase 1. Ready to build sidecar deployment.
- **Axiom** (axiom-marbell): Cross-part mapping table, tier compliance matrix, end-to-end example, OQ consolidation.

**Changes from original draft** (18 items integrated):

| # | Change | Source |
|---|--------|--------|
| 1 | Complete state machine transitions (exhaustive table with notes) | Sage + Kelvin |
| 2 | Pause timeout produces `cancelled`, not `failed` | Sage |
| 3 | SSE reconnection via `Last-Event-Id` with `id:` on all events | Sage |
| 4 | Schema convention documented (camelCase WebMCP vs snake_case API) | Sage |
| 5 | Redirect tracks lineage (same as retask) | Sage |
| 6 | Sidecar failure handling (heartbeat loss detection, task recovery) | Sage |
| 7 | Pull dispatch model (NAT-friendly, outbound-only) | Kelvin |
| 8 | `AGENT_SUSPENDED` error code (intentional vs connectivity failure) | Kelvin |
| 9 | Per-workflow `result_retention_days` + metadata vs payload split | Sage + Kelvin |
| 10 | Fleet API `server_public_key` in manifest + callback signature parity | Sage + Kelvin |
| 11 | Heartbeat uses Ed25519 auth + SSE as implicit heartbeat | Sage + Kelvin |
| 12 | Deprecated workflows get `DEPRECATED_PATH (301)` redirect | Kelvin |
| 13 | Sidecar basic endpoints in Phase 1, enhanced in Phase 2 | Kelvin + Sage |
| 14 | `task_events` partitioning by `created_at` from day one | Kelvin |
| 15 | Pattern 12 compliance footnote ("compliant by design") | Sage |
| 16 | RFC 0 operation → endpoint mapping table elevated to Section 2.4 | Axiom |
| 17 | Full end-to-end lifecycle example (Section 9) | Axiom |
| 18 | Sidecar health endpoint (`/fleet/health`) | Kelvin |

---

*This RFC defines the foundation for distributed task execution across the fleet, implementing the Principal Orchestrator Pattern -- six operations (create, monitor, interrupt, context, review, retask) that give a principal full lifecycle authority over remote tasks. The interface is recursive: the same verbs apply at every level of the delegation hierarchy. Phase 1 delivers end-to-end task dispatch (including the sidecar execution bridge), Phase 2 adds real-time monitoring and the full Principal Orchestrator Pattern, and Phase 3 achieves full Gold tier compliance.*
