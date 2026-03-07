# RFC 2: Pi.dev Sub-Agent Architecture

**Status**: Resolved (synthesized from team review)
**Author**: Sage (finml-sage)
**Reviewers**: Nexus (nexus-marbell), Kelvin (mlops-kelvin), Axiom (axiom-marbell)
**Date**: 2026-03-07
**Standard**: Agentic API Standard Gold Tier target (all 20 patterns)
**Depends on**: RFC 0 (Principal Orchestrator Pattern), RFC 1 (Agentic Task API)

---

## 1. Problem

Fleet-api (RFC 1) dispatches tasks to remote agents. But a single Claude Code instance on a remote VM is fragile -- one agent, one context window, one failure point. If the task is complex, the agent burns through context. If it crashes, state is lost. If it needs multiple capabilities (coding, git operations, rule management), a single agent handles all of them poorly.

We need a pattern for how remote execution actually works. Not one agent -- a **squad**.

---

## 2. Definition

A **Pi.dev Sub-Agent Squad** is a multi-agent unit deployed on a pi.dev execution environment that receives tasks from fleet-api and executes them using internal specialization. The squad orchestrator is a Principal Orchestrator (RFC 0) to its specialist agents.

```
Fleet API (centralized)
  |
  +-- POST /workflows/{id}/run
  |
  +-- Pi.dev Squad (remote)
        +-- Squad Orchestrator --- receives task, decomposes, delegates
        |     +-- Rules/Skills Agent --- maintains .claude/rules/, .claude/skills/
        |     +-- Task Specialist(s) --- does the actual work (coding, research, etc.)
        |     +-- GitOps Agent --- commits, PRs, branch management
        |
        +-- Fleet Agent Sidecar --- polls Fleet API, streams events back
```

### 2.1 The Sidecar Is Transport, Not an Operation

The Fleet Agent sidecar appears in the diagram alongside the squad's agents but is **not** a squad member and **not** a seventh operation. It is transport infrastructure -- the bridge between Fleet API's HTTP interface and the squad orchestrator's local execution.

This is the same component defined in RFC 1 Section 7 (`src/fleet_agent/`). RFC 1 specifies its polling behavior, event streaming, and failure handling. RFC 2 specifies how the squad orchestrator interacts with it internally.

| Concern | Sidecar | Squad Orchestrator |
|---------|---------|-------------------|
| Fleet API communication | Owns the connection (polls, streams SSE) | Never contacts Fleet API directly |
| Task reception | Polls `GET /agents/{id}/tasks/pending`, receives task payloads | Receives task from sidecar, decomposes it |
| Event streaming | Pushes SSE events to Fleet API via `POST /tasks/{id}/events` | Emits events locally; sidecar picks them up |
| Context injection | Polls for pending context payloads from Fleet API | Receives context from sidecar, routes to relevant specialists |
| Pause/resume/cancel | Polls for signals from Fleet API, delivers to orchestrator | Executes the signal (pauses/resumes/cancels specialists) |
| Redirect | Receives redirect signal, cancels current task, starts new one | Tears down current specialists, begins new decomposition |
| Health | Exposes `GET /fleet/health` for systemd watchdog | Not responsible for health reporting |
| Heartbeat | Sends `POST /agents/{id}/heartbeat` to Fleet API (60-second interval for squads, vs 5-minute for standalone agents) | Not responsible for heartbeats |

This separation means a squad orchestrator does not need HTTP client logic. It speaks to the sidecar through local IPC (stdin/stdout, Unix socket, or filesystem -- implementation-defined). The sidecar handles all network concerns.

### 2.2 Pause State Boundary

RFC 1 Section 7.5 shows the sidecar streaming a `paused` status event back to Fleet API. RFC 2 clarifies what crosses the boundary:

- **Fleet API knows**: Task status (`paused`), progress percentage at pause time, whether the state is resumable, TTL expiry timestamp.
- **Fleet API does NOT know**: Execution internals -- which specialists were running, partial results, in-memory state.
- **Squad filesystem holds**: Full execution state -- specialist snapshots, partial results, orchestrator decomposition plan, context injection queue.

On resume, Fleet API sends the resume signal through the sidecar. The squad orchestrator reconstructs from its own filesystem snapshot. Fleet API never stores or transmits execution internals -- it holds a handle, not the state.

This is consistent with RFC 0 Section 4.3.1 (Pause State Ownership): "State lives with the executor."

---

## 3. Squad Composition

### 3.1 Squad Orchestrator

The orchestrator is the only agent that communicates with Fleet API (via the sidecar). It:

1. **Receives** the task from fleet-api (Create operation)
2. **Decomposes** into sub-tasks for specialists
3. **Delegates** using the same six operations (Create/Monitor/Interrupt/Context/Review/Retask)
4. **Synthesizes** results from specialists into a coherent deliverable
5. **Reports** progress and results back through the sidecar

The orchestrator does NOT do specialist work. It coordinates. This is the same separation the main team maintains -- orchestrators orchestrate.

### 3.2 Rules/Skills Agent

Maintains the squad's institutional memory:

- **Rules** (`.claude/rules/`): Behavioral constraints, project-specific policies, lessons learned
- **Skills** (`.claude/skills/`): Domain knowledge, templates, reference material
- **CLAUDE.md**: Project instructions that shape every agent in the squad

This agent ensures the squad learns across tasks. Without it, every task starts from zero -- the squad is stateless. With it, the squad accumulates knowledge like the main team does.

**Update triggers**: After task completion, after errors, after corrections from the principal. The Rules/Skills agent watches for patterns that should become permanent knowledge.

**Knowledge invalidation**: Knowledge can go stale. The Rules/Skills agent must also **remove or update** knowledge that is contradicted by new evidence. Mechanisms:

- **Contradiction detection**: When a task fails and the post-mortem traces the failure to a learned rule, the rule is flagged for review or removed.
- **Principal correction**: When the principal injects a `correction` context type, the Rules/Skills agent checks whether any existing rules conflict with the correction and updates accordingly.
- **Staleness TTL**: Rules learned from a single task carry a confidence flag. Rules confirmed across 3+ tasks are promoted to permanent. Single-task rules expire after a configurable period (default: 30 days) unless reinforced.

This mirrors how the main team handles knowledge invalidation through workbench sessions, circle nights, and manual review -- but automated for squads that lack those rituals.

**Relationship to agent-memory**: The main team uses a shared agent-memory repo (BM25 search, structured markdown, frontmatter). The Rules/Skills agent manages `.claude/rules/` and `.claude/skills/` -- a squad-local knowledge system. These are separate by design:

- Squad knowledge is scoped to the squad's domain and does not pollute the main team's memory.
- Knowledge transfer from squads to the main team (or between squads) is handled through the knowledge transfer mechanism described in Section 9.2, not through direct agent-memory writes.
- If a squad discovers something valuable enough for the main team, it reports the finding in its task result. The principal decides whether to promote it to team memory.

### 3.3 Task Specialist(s)

The agents that do the actual work. One or more, depending on task complexity:

- **Single specialist**: Simple tasks. One agent codes/researches/writes.
- **Multiple specialists**: Complex tasks. Parallelizable sub-tasks get their own agents.

Specialists are ephemeral -- they're created for a task and may not persist. The knowledge they generate is captured by the Rules/Skills agent before they terminate.

**Squad size limit**: Maximum 4 concurrent specialists per squad (recommended). The orchestrator's context window is the real bottleneck -- not compute. Beyond 4, orchestration overhead (context management, result synthesis, conflict resolution) dominates execution time. Start here, measure, then adjust.

### 3.4 GitOps Agent

Handles all git operations:

- Branch creation and management
- Commits with proper attribution
- Pull request creation and updates
- Conflict resolution
- Repository hygiene (no large files, no secrets, clean history)

**Why separate?** Git operations are a cross-cutting concern. Every specialist needs commits, but git discipline (clean history, meaningful messages, proper branching) is a skill that shouldn't be duplicated across specialists. One agent owns it.

### 3.5 Lateral Communication

Specialists communicate **only through the orchestrator**. There is no peer-to-peer channel between squad members.

If Specialist A discovers something relevant to Specialist B, it reports the finding to the orchestrator, which injects it as context into Specialist B (Context operation). This keeps the orchestrator as the single point of coordination and prevents information flows that the orchestrator can't observe or control.

This is the same model as vertical authority in the main team: swarm messages between peers exist because peers are independent principals. Squad specialists are not independent -- they are subordinate to the orchestrator. Subordinates communicate through their principal.

### 3.6 Six Operations at the Squad Level

The Principal Orchestrator Pattern (RFC 0) holds recursively at the squad level. The squad orchestrator is a principal to its specialists, using the same six operations.

| # | RFC 0 Operation | Squad Implementation |
|---|-----------------|---------------------|
| 1 | **Create** | Orchestrator decomposes the fleet-api task into specialist sub-tasks |
| 2 | **Monitor** | Orchestrator watches specialist progress; sidecar streams aggregated events to fleet-api |
| 3 | **Interrupt** | Orchestrator can pause, redirect, or kill any specialist |
| 4 | **Context** | Orchestrator routes injected context from fleet-api to relevant specialists |
| 5 | **Review** | Orchestrator reviews specialist output before synthesizing the deliverable |
| 6 | **Retask** | Orchestrator can retry or adjust specialist sub-tasks with inherited context |

The recursive property holds. The six operations are the same at every level. The sidecar is not a seventh operation -- it is the transport adapter that maps Fleet API's HTTP interface to the orchestrator's local execution, the same way a terminal maps Dan's keystrokes to Sage's Claude Code session.

---

## 4. Identity and Attribution

### 4.1 Shared GitHub Account

All squads share a single GitHub account (e.g., `fleet-worker` or similar). Individual agents within a squad are NOT separate GitHub users.

**Self-identification** happens in commit messages and PR descriptions:

```
feat(indicators): add VWAP calculation for intraday signals

Squad: api-indicators-refactor
Orchestrator: squad-orch-01
Specialist: task-spec-03 (Python/FastAPI)
Task: fleet-api/tasks/abc123

Co-Authored-By: Fleet Worker <fleet-worker@marbell.com>
```

The commit is attributed to the shared account. The extended message identifies which squad, which agent, and which task produced it. This is traceable without requiring N GitHub accounts.

### 4.2 Why Not Individual Accounts?

- GitHub accounts cost money at scale
- Permission management becomes combinatorial
- The squad is the unit of accountability, not the individual agent
- Traceability is achieved through commit metadata, not account identity

---

## 5. Lifecycle

### 5.1 Task Reception

1. Fleet Agent sidecar polls `GET /agents/{id}/tasks/pending` on Fleet API (RFC 1 Section 7.1)
2. Sidecar receives the task payload and passes it to the Squad Orchestrator
3. Orchestrator reads the task definition: intent, constraints, deliverables, authority scope
4. Orchestrator creates a plan and begins delegation

**Endpoint consistency**: The sidecar receives tasks by polling the Fleet API's pending-tasks endpoint -- the same pull model defined in RFC 1 Section 7.1. There is no separate `POST /fleet/execute` endpoint. The sidecar has only one local endpoint: `GET /fleet/health` for systemd monitoring.

### 5.2 Execution

1. Orchestrator creates sub-tasks for specialists (Create)
2. Specialists work, streaming progress through the orchestrator (Monitor)
3. Orchestrator can interrupt, inject context, or retask specialists as needed
4. GitOps agent handles all repository operations on behalf of specialists
5. Sidecar streams progress events back to fleet-api (outbound SSE push)

### 5.3 Context Injection (mid-task)

When fleet-api sends `POST .../context`:

1. Sidecar polls and receives the pending context payload from Fleet API
2. Passes to Squad Orchestrator
3. Orchestrator decides which specialists are affected
4. Routes the context to relevant specialists (Context operation)
5. Specialists integrate without restarting
6. Sidecar streams `context_injected` event back to fleet-api

This is the recursive property in action -- fleet-api injects context into the squad, the squad orchestrator injects context into the specialist. Same operation, different level.

The routing step (step 3) is the value RFC 2 adds over RFC 1's "implementation-defined" context delivery. RFC 1 defines the transport; RFC 2 defines the decision logic inside the squad.

### 5.4 Redirect

When fleet-api sends a redirect signal:

1. Sidecar polls and receives the redirect from Fleet API (cancel + new task with modified constraints)
2. Squad Orchestrator receives the cancellation
3. Orchestrator terminates all active specialists (Interrupt/cancel)
4. Rules/Skills agent captures any partial learnings before teardown
5. GitOps agent cleans up any in-progress branches (no abandoned PRs)
6. Sidecar confirms cancellation of the old task (`redirected` terminal status)
7. Sidecar receives the new task (next poll cycle)
8. Orchestrator begins fresh decomposition with the new constraints

Redirect at the squad level does NOT reuse existing specialists. It is a full teardown and restart. The new task inherits lineage from the original (same as RFC 1's `redirected_from` field), so the chain of course corrections is traceable.

### 5.5 Completion

1. Specialists report results to the Orchestrator (Review)
2. Orchestrator synthesizes into a single deliverable
3. Rules/Skills agent captures lessons learned
4. GitOps agent ensures all changes are committed and PR'd
5. Orchestrator reports final result through sidecar
6. Fleet-api marks task complete

### 5.6 Failure

**Specialist failure**:

1. Orchestrator receives the failure
2. Decides: retry, retask with adjustments, or escalate
3. If retrying: new specialist, same sub-task, inherited context (Retask)
4. If escalating: reports failure through sidecar to fleet-api
5. Fleet-api's principal decides next steps

**Internal retry depth limit**: The squad orchestrator enforces its own retry limit for internal specialist failures (default: 3). RFC 1 defines `FLEET_RETASK_MAX_DEPTH` (default: 10) for retask chains between the principal and the squad. The squad's internal retries do NOT count against the fleet-level retask depth -- they are invisible to the principal. Without an internal limit, a failing sub-task could loop indefinitely within the squad.

**Orchestrator failure**:

1. Sidecar detects loss of heartbeat (internal heartbeat, 10-second interval)
2. Sidecar reports failure to fleet-api with last known state
3. Fleet-api can retask to a new squad or escalate to the principal

**Sidecar failure**: See Section 9.7 (Resolved Design Decisions).

---

## 6. State Management

### 6.1 What Lives Where

| State | Location | Persistence |
|-------|----------|-------------|
| Task definition | Fleet-api (PostgreSQL) | Permanent |
| Execution progress | Sidecar --> SSE stream to Fleet API | Ephemeral (logged in `task_events`) |
| Squad knowledge | `.claude/rules/`, `.claude/skills/` | Persistent across tasks |
| Specialist context | In-memory (agent context window) | Task-scoped |
| Code changes | Git (branches, commits) | Permanent |
| Task results | Fleet-api (via sidecar) | Per-workflow `result_retention_days` |

### 6.2 Pause/Resume

When fleet-api sends a pause:

1. Sidecar polls and receives the pause signal
2. Sidecar notifies Orchestrator
3. Orchestrator pauses all active specialists
4. Specialists save state (current progress, partial results) to the squad's filesystem
5. Squad enters paused state
6. On resume: Orchestrator restores specialists with saved state from filesystem

State during pause lives in the squad's filesystem -- not in fleet-api. Fleet API receives a summary (`progress`, `resumable`, `state_ttl_seconds`) but not the execution internals. The squad is responsible for its own state. This is consistent with RFC 0 Section 4.3.1 and the reconciliation in Section 2.2 above.

### 6.3 Filesystem Cleanup

Between tasks, the squad filesystem accumulates working directories, build artifacts, and temporary files. Without garbage collection, disk fills.

**Retention policy**:

| Directory | Between Tasks | On Squad Teardown |
|-----------|---------------|-------------------|
| `.claude/rules/`, `.claude/skills/`, `CLAUDE.md` | Retained (knowledge) | Snapshot preserved for hibernate |
| Git repos (`.git/`, working trees) | Retained (history) | Snapshot preserved for hibernate |
| Specialist working directories | Purged | Purged |
| Build artifacts, caches, temp files | Purged | Purged |
| Sidecar logs | Rotated (keep last 3 tasks) | Purged |

---

## 7. Pi.dev Execution Environment

### 7.1 What Pi.dev Provides

- Isolated compute environment per squad
- Claude Code runtime with tool access
- Filesystem persistence across tasks (for rules/skills accumulation)
- Network access for git operations and fleet-api communication
- Resource limits (CPU, memory, storage) per squad

### 7.2 What Pi.dev Does NOT Provide

- Inter-squad communication (squads don't talk to each other -- they talk to fleet-api)
- Shared filesystem between squads (each squad is isolated)
- Direct access to the main team's VMs (all interaction through fleet-api)

### 7.3 Squad Provisioning

Squads are provisioned on demand:

1. Fleet-api receives a task that requires remote execution
2. Fleet-api provisions a pi.dev environment (or reuses an existing one)
3. Squad boots with: CLAUDE.md, initial rules/skills, sidecar configuration
4. Sidecar registers with fleet-api (sends first heartbeat)
5. Task is dispatched

**Boot sequence -- initial knowledge provisioning**:

The initial rules/skills come from the **workflow registration**. When a workflow is registered (`POST /workflows`), the registration includes a `bootstrap` field pointing to a git repo or artifact containing the CLAUDE.md, initial rules, and initial skills for squads executing that workflow.

```json
{
  "id": "wf-code-review",
  "bootstrap": {
    "repo": "https://github.com/nexus-marbell/fleet-squad-templates.git",
    "path": "code-review/",
    "ref": "main"
  }
}
```

Cold squads clone the bootstrap on first boot. Warm squads already have it (plus accumulated knowledge). This solves the cold-start problem: a cold squad can deliver quality work because it starts with curated knowledge, not an empty filesystem.

### 7.4 Warm, Cold, and Hibernate Modes

**Cold squads**: Provision fresh, execute task, tear down after. No accumulated knowledge, but no idle cost.

- **When**: Task type is infrequent or one-off AND knowledge doesn't compound.
- **Examples**: One-time migrations, exploratory research, ad-hoc analysis.

**Warm squads**: Keep alive between tasks. Retain accumulated knowledge (rules/skills) and start faster.

- **When**: Task type recurs daily or more frequently AND accumulated knowledge saves >10 minutes per task.
- **Examples**: Recurring code review squads, CI/deployment squads, monitoring squads.
- **Risk**: Staleness -- knowledge from Task A may be wrong for Task B. Mitigated by the knowledge invalidation mechanism in Section 3.2.

**Hibernate** (recommended default): Persist filesystem snapshot, tear down compute, restore on demand.

- **Startup latency**: ~30 seconds (vs instant for warm, vs ~2 minutes for cold with bootstrap clone).
- **Idle cost**: Zero (no compute, only storage for the snapshot).
- **Knowledge**: Preserved from previous tasks.
- **Examples**: Most recurring workflows that don't need instant response.

The decision framework:

| Factor | Cold | Hibernate | Warm |
|--------|------|-----------|------|
| Knowledge accumulation | None | Preserved | Preserved |
| Idle cost | Zero | Storage only | Full compute |
| Startup latency | ~2 min (clone) | ~30 sec (restore) | Instant |
| Staleness risk | None | Medium | High |
| Best for | One-offs | Most workflows | High-frequency |

Pi.dev pricing will determine the exact cost thresholds, but the decision framework holds regardless of pricing.

### 7.5 Warm Squad Identity and Lineage

If Squad X handles task-001 and then task-002, it is the "same" squad with accumulated state. This creates an observability concern: if task-002 produces poor results, was the squad contaminated by prior task context?

**Squad lineage tracking**: Each squad maintains a `squad_history` log:

```json
{
  "squad_id": "squad-code-review-01",
  "mode": "warm",
  "tasks_executed": ["task-001", "task-002", "task-003"],
  "knowledge_updates": [
    { "task": "task-001", "rules_added": 2, "rules_removed": 0 },
    { "task": "task-002", "rules_added": 1, "rules_removed": 1 }
  ],
  "last_bootstrap_version": "main@abc1234",
  "last_config_update": "2026-03-07T10:00:00Z"
}
```

This log is included in the sidecar's heartbeat payload. The principal can inspect any squad's history via `GET /agents/{id}` to determine whether accumulated state may have influenced a result. The commit metadata pattern (Section 4.1) also traces each commit to a specific task, providing git-level lineage.

---

## 8. Authority Model

```
Dan (Human Principal)
  +-- Sage (Agent Orchestrator)
        +-- Fleet-api (task dispatch)
              +-- Squad Orchestrator (Principal to specialists)
                    +-- Rules/Skills Agent (authority: knowledge management)
                    +-- Task Specialist (authority: task-scoped work)
                    +-- GitOps Agent (authority: repository operations)
```

Each level delegates authority downward with explicit scope:

- **Sage --> Fleet-api**: "Execute this task on a remote squad. The squad may decide implementation details. Escalate if requirements are ambiguous."
- **Fleet-api --> Squad Orchestrator**: "Here is the task definition. You have authority to decompose and delegate internally. Report progress via SSE."
- **Squad Orchestrator --> Specialist**: "Implement this sub-task. You may decide coding patterns within the constraints. Escalate if you encounter architectural decisions."

Authority narrows at each level. A specialist cannot make architectural decisions. A squad cannot change the task definition. Only the principal at each level can expand scope.

### 8.1 Escalation Chain

Escalation flows through fleet-api. Squads do NOT have direct swarm access.

```
Specialist (blocked)
  --> Squad Orchestrator (decides: handle or escalate)
      --> Sidecar (SSE escalation event)
          --> Fleet API (stores, streams to principal's SSE)
              --> Principal (Sage) (decides: context injection, retask, or cancel)
```

Adding a swarm escape hatch would create two authority paths, which is how conflicting instructions happen. If latency is a concern, the answer is faster fleet-api response times, not a bypass channel. The chain model (RFC 0 Section 3.1) propagates; it does not fork.

---

## 9. Resolved Design Decisions

These were open questions during drafting. Resolved by team review (Nexus, Kelvin -- positions documented in Issue #4 comments).

### 9.1 Squad Size Limits (OQ #1)

**Decision: Maximum 4 concurrent specialists per squad. Start here, measure, adjust.**

The orchestrator's context window is the real bottleneck -- not compute. Beyond 4 specialists, orchestration overhead (context management, result synthesis, conflict resolution) dominates execution time. From main-team experience with Claude Code subagents: 3-5 concurrent specialists is the sweet spot.

Resolved by: Kelvin (initial recommendation with rationale), Nexus (confirmed).

### 9.2 Knowledge Transfer Between Squads (OQ #2)

**Decision: Start with git repo (Option A). Graduate to agent-memory (Option B) when volume justifies it.**

Fleet-api is a task dispatch system, not a knowledge broker. Knowledge transfer between squads uses a dedicated mechanism:

**Phase 1 -- Git repo**: A shared `squad-knowledge` repository. Rules/Skills agents commit curated rules to this repo after task completion. Other squads pull on boot (or on a schedule for warm squads).

**Phase 2 -- Agent-memory integration**: Squads write to agent-memory with a `squad/` namespace. Other squads search before starting work. Same pattern the main team uses for cross-agent learning. Upgrade when the volume of cross-squad knowledge exceeds what a flat git repo can serve efficiently.

Both options keep fleet-api out of the knowledge path. Fleet-api dispatches tasks; knowledge flows through its own channel.

Resolved by: Kelvin (Option A/B phasing), Nexus (confirmed fleet-api is not a knowledge broker).

### 9.3 Squad Specialization (OQ #3)

**Decision: General-purpose squads initially. Specialization emerges from warm squad knowledge accumulation.**

Don't pre-specialize squads ("Python squad," "frontend squad") before there's evidence that specialization helps. Warm squads naturally specialize through accumulated rules/skills -- a squad that handles 10 Python tasks will have better Python rules than a cold squad. This is emergent specialization, not prescribed.

If explicit specialization proves valuable, it can be encoded in the workflow's `bootstrap` configuration: different bootstrap repos for different domains.

Resolved by: Sage (original position), Kelvin (confirmed with warm-squad-as-specialization insight).

### 9.4 Cost Model (OQ #4)

**Decision: Hibernate as default mode. Warm only for high-frequency workflows. Cold for one-offs.**

See Section 7.4 for the full decision framework. Pi.dev pricing determines exact thresholds. The hibernate mode (persist snapshot, tear down compute) is the recommended default -- it captures the knowledge benefit of warm squads without the idle compute cost.

Resolved by: Kelvin (hibernate as third option), Sage (confirmed as default).

### 9.5 Security Boundary (OQ #5)

**Decision: Sandboxed specialist working directories. Shared access to Rules/Skills files. Isolated sidecar config.**

| Resource | Rules/Skills Agent | Specialists | GitOps Agent | Sidecar |
|----------|-------------------|-------------|--------------|---------|
| `.claude/rules/`, `.claude/skills/`, `CLAUDE.md` | Read/Write | Read | Read | No access |
| Specialist working directories | No access | Own dir only (isolated) | No access | No access |
| GitOps workspace (repo) | No access | Read | Read/Write | No access |
| Sidecar config (fleet-api credentials) | No access | No access | No access | Exclusive |

The key principle: specialists can read knowledge (rules/skills) and see repo state, but cannot write to each other's directories or access fleet-api credentials. The sidecar's configuration (including the agent's Ed25519 private key) is inaccessible to all squad agents -- it runs in a separate process with separate filesystem permissions.

Resolved by: Kelvin (filesystem permission model), Nexus (confirmed sidecar isolation).

### 9.6 Escalation Chain (OQ #6)

**Decision: Fleet-api is the only escalation channel. No direct swarm access from squads.**

See Section 8.1 for the full rationale. Direct swarm access would create two authority paths. The chain model propagates; it does not fork.

Resolved by: Kelvin (initial position), Nexus (confirmed with RFC 0 chain model reference).

### 9.7 Sidecar Health and Recovery (OQ #7, added by Nexus)

**Decision: Sidecar is stateless and restartable. Fleet API detects absence via missing polls.**

The sidecar maintains no execution state of its own -- it is a relay between Fleet API and the squad orchestrator. If the sidecar crashes:

1. **Detection**: Fleet API detects absence when no poll arrives within the heartbeat interval (60 seconds for squads). After the timeout, the agent is marked unreachable.
2. **In-flight SSE streams**: Drop without a terminal event. Fleet API waits for reconnection (using `Last-Event-Id` from the last received event). If no reconnection within the timeout, the task transitions to `failed` with error code `EXECUTOR_UNREACHABLE`.
3. **Recovery**: Sidecar restarts via systemd (`Restart=always`). On restart, it polls for any in-flight tasks and attempts to reconnect. The squad orchestrator is still running -- only the relay was lost. If the orchestrator is also down, the sidecar reports task failure.
4. **Installation**: `pipx install fleet-agent` from the `fleet-api` monorepo. Systemd service on each VM. Same pattern as ACP.

The sidecar's statelessness is what makes it restartable. All durable state lives in Fleet API (task metadata) and the squad filesystem (execution state).

Resolved by: Nexus (identified the gap), Kelvin (installation pattern and heartbeat interval).

### 9.8 Warm Squad Versioning (OQ #8, added by Nexus)

**Decision: Bootstrap version check on each task start. Pull-on-mismatch.**

When a warm squad receives a new task, the sidecar checks whether the workflow's `bootstrap` configuration has changed since the last task:

1. Compare `bootstrap.ref` (git commit hash) against the squad's `last_bootstrap_version`.
2. If they match: proceed with existing knowledge.
3. If they differ: pull the updated bootstrap before starting the task. New rules override old ones; new skills are merged.

This ensures warm squads receive configuration updates without requiring teardown and re-provisioning. The check is lightweight (one git fetch + compare) and happens before task execution begins.

Resolved by: Nexus (identified the gap), Kelvin (pull-on-mismatch mechanism).

---

## 10. Relationship to Swarm

Squads are NOT swarm peers. They are subordinate executors:

| Dimension | Swarm Peers | Pi.dev Squads |
|-----------|-------------|---------------|
| Relationship | Peer-to-peer | Principal-subordinate |
| Communication | Swarm messages | Fleet-api tasks |
| Authority | Independent principals | Delegated, scoped |
| Persistence | Permanent (long-running agents) | Task-scoped, warm, or hibernate |
| Identity | Named agents (Nexus, Kelvin, Axiom) | Shared account, squad ID |
| Failure recovery | Self-recover (compaction, agent-memory) | Fleet-api intervention required |

The main team (Sage, Nexus, Kelvin, Axiom) coordinates via swarm. Squads execute via fleet-api. These are different interaction models for different relationships.

---

## 11. Worked Example: Full Squad Lifecycle

This traces a single fleet-api task through the squad's internal decomposition and execution. The scenario mirrors the code review example from RFC 0 Section 6 and RFC 1 Section 9, now showing what happens **inside** the squad.

```
FLEET TASK: task-review-001 (wf-code-review)
SQUAD: squad-code-review-01 (warm, 3 prior tasks)
PRINCIPAL: Sage (via fleet-api)

================================================================
1. TASK RECEPTION
================================================================

Fleet Agent sidecar polls:
  GET /agents/squad-code-review-01/tasks/pending
  --> 200 OK [{
        "task_id": "task-review-001",
        "workflow_id": "wf-code-review",
        "input": { "pr_url": "https://github.com/.../pull/42", "standards": ["srp", "owasp"] },
        "constraints": { "max_duration_seconds": 300 }
      }]

Sidecar passes task to Squad Orchestrator.

================================================================
2. DECOMPOSITION (Create -- squad level)
================================================================

Orchestrator analyzes the task:
  - PR has 247 lines across 8 files
  - 3 files are auth middleware (security-relevant)
  - 5 files are business logic (SRP-relevant)
  - Checks Rules/Skills agent: has OWASP checklist from task-review-098

Orchestrator creates 3 sub-tasks:
  Sub-task A: "Review auth middleware (3 files) against OWASP Top 10"
              --> Specialist-01 (security focus)
  Sub-task B: "Review business logic (5 files) against SRP"
              --> Specialist-02 (architecture focus)
  Sub-task C: "Check dependency versions for known CVEs"
              --> Specialist-01 (after sub-task A completes)

Sub-tasks A and B are independent --> launched in parallel.

================================================================
3. EXECUTION (Monitor -- squad level)
================================================================

Specialist-01 works on auth review...
  Orchestrator monitors progress.
  Sidecar streams: event: progress { "progress": 25, "message": "Auth review: 1/3 files" }

Specialist-02 works on SRP review...
  Orchestrator monitors progress.
  Sidecar streams: event: progress { "progress": 30, "message": "SRP review: 2/5 files" }

================================================================
4. CONTEXT INJECTION (Context -- squad level)
================================================================

Principal (Sage) injects context via fleet-api:
  POST .../tasks/task-review-001/context
  { "context_type": "constraint", "payload": { "message": "Also flag any eval() usage" } }

Sidecar polls, receives pending context.
Orchestrator routes to Specialist-01 (security-relevant).
Specialist-02 is not affected --> no interruption.

Sidecar streams: event: context_injected { "sequence": 1, "message": "Constraint applied" }

================================================================
5. SPECIALIST COMPLETION (Review -- squad level)
================================================================

Specialist-02 finishes SRP review: 4 findings.
  Orchestrator reviews output --> accepts.

Specialist-01 finishes auth review: 6 findings.
  Orchestrator reviews output --> accepts, launches sub-task C.

Specialist-01 runs CVE check: 2 findings (outdated cryptography package).
  Orchestrator reviews output --> accepts.

================================================================
6. SYNTHESIS AND DELIVERY
================================================================

Orchestrator synthesizes:
  - 12 total findings (6 auth + 4 SRP + 2 dependency)
  - Deduplicates (1 finding appeared in both auth and SRP reviews)
  - Final: 11 unique findings, "3 critical, 4 warning, 4 info", pass: false

Rules/Skills agent captures:
  - New rule: "cryptography < 42.0 has CVE-2026-XXXX"
  - Updated skill: OWASP checklist now includes eval() constraint

GitOps agent: no code changes in this task (review only).

Sidecar streams:
  event: completed { "result": { "findings": [...11 items...], "pass": false } }

Fleet-api marks task-review-001 as completed.
```

---

## 12. Compliance

### 12.1 RFC 0 Compliance (Principal Orchestrator Pattern)

The six operations hold recursively at the squad level. The compliance table from Section 3.6 maps each operation to its squad implementation.

**Completeness verification**: The same edge cases verified for RFC 0 (Section 2) hold at the squad level:

- "Discover specialist capabilities" -- Not a task lifecycle operation. The orchestrator knows its specialists by construction.
- "Specialist errored" -- Covered by Monitor (orchestrator detects) and Retask (orchestrator retries).
- "Cancel a paused specialist" -- Covered. Interrupt handles all three modes at the squad level.

### 12.2 RFC 1 Compliance (Agentic Task API)

The squad's sidecar implements the Fleet Agent defined in RFC 1 Section 7. Compliance points:

| RFC 1 Component | Squad Implementation |
|-----------------|---------------------|
| Pull model (S7.1) | Sidecar polls `GET /agents/{id}/tasks/pending` |
| Sidecar architecture (S7.2) | Fleet Agent installed via `pipx install fleet-agent`, systemd service |
| Internal endpoints (S7.3) | `GET /fleet/health` only -- all other communication is outbound |
| Context injection flow (S7.4) | Sidecar polls for context, orchestrator routes to specialists |
| Pause/resume flow (S7.5) | Sidecar polls for signals, orchestrator manages specialist state |
| Failure handling (S7.6) | Sidecar detects orchestrator failure, reports to fleet-api |

### 12.3 Agentic API Standard

The squad itself does not expose HTTP endpoints externally (the sidecar handles all fleet-api communication). However, the squad's interaction model follows the standard's principles:

- **Pattern 1 (Manifest)**: The workflow's registration serves as the squad's manifest -- capabilities, schemas, and constraints are declared at registration time.
- **Pattern 3 (Standard Errors)**: Specialist failures reported through the sidecar follow the standard error format.
- **Pattern 6 (Self-Describing)**: Workflow `input_schema`/`output_schema` describe what the squad accepts and produces.
- **Pattern 14 (Anti-Patterns)**: Workflow `anti_patterns` document known failure modes for the squad's task type.

Gold Tier compliance is demonstrated at the Fleet API level (RFC 1 Section 6). The squad inherits this compliance by operating behind the Fleet API's interface.

---

## 13. RFC Cross-References

| RFC | Relationship to RFC 2 |
|-----|----------------------|
| **RFC 0** (Principal Orchestrator Pattern) | RFC 2 proves the recursive property: the same six operations that govern Human-to-Agent and Agent-to-Remote-Agent interactions also govern Squad-Orchestrator-to-Specialist interactions. The pattern holds at every level of the authority chain (RFC 0 Section 3). |
| **RFC 1** (Agentic Task API) | RFC 2's sidecar is the Fleet Agent defined in RFC 1 Section 7. RFC 2 fills the "implementation-defined" gap in RFC 1's context delivery by specifying the orchestrator's routing logic. RFC 2's pause state boundary (Section 2.2) reconciles with RFC 1's pause flow (Section 7.5). Redirect handling (Section 5.4) implements the `redirected` status from RFC 1's state machine (Section 2.5). |

---

## 14. Provenance

This document synthesizes the original RFC 2 draft (Sage, Issue #4 on `nexus-marbell/fleet-api`) with review comments from the full team:

- **Nexus** (nexus-marbell): 8-section protocol design review -- RFC 0/RFC 1 alignment verification, sidecar identity reconciliation (RFC 1 Section 7 = RFC 2 sidecar), pause state boundary analysis, redirect handling gap, sidecar/orchestrator responsibility matrix, Rules/Skills vs agent-memory relationship, endpoint naming consistency, specialist retry depth limit, boot sequence specification, warm squad staleness and lineage tracking, lateral communication model, two new OQs (sidecar health, warm squad versioning), comparison table additions.
- **Kelvin** (mlops-kelvin): Infrastructure review -- OQ resolutions for squad size (4 max), knowledge transfer (git then agent-memory), cost model (warm/cold/hibernate), security boundary (filesystem permission model), escalation chain (fleet-api only), sidecar installation (pipx + systemd), heartbeat interval (60s for squads), filesystem cleanup policy.
- **Axiom** (axiom-marbell): Shadow review -- Gold Tier reference to RFC 1 patterns, specialist-to-six-ops mapping table, lifecycle worked example recommendation.

---

*A squad is not a single agent pretending to be capable. It is a team that knows its roles -- the Principal Orchestrator Pattern (RFC 0), deployed at scale.*
