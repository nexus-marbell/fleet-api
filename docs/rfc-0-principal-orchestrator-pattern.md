# RFC 0: The Principal Orchestrator Pattern

**Status**: Resolved (synthesized from team review)
**Author**: Sage (finml-sage)
**Reviewers**: Nexus (nexus-marbell), Kelvin (mlops-kelvin), Axiom (axiom-marbell)
**Date**: 2026-03-07
**Standard**: Agentic API Standard Gold Tier target (all 20 patterns)
**Depends on**: None (foundational)
**Depended on by**: RFC 1 (Agentic Task API), RFC 2 (Pi.dev Sub-Agent Architecture)

---

## 1. Problem

We have three interaction models that are structurally identical but implemented differently:

1. **Human to Agent**: Dan gives Sage a directive, monitors output in real time, interrupts with corrections, adds context mid-task, reviews results, retasks with refinement.
2. **Agent to Local Subagent**: Sage creates a task, launches a background subagent, monitors via TaskOutput, stops via TaskStop, retasks with a new prompt.
3. **Agent to Remote Agent**: Not yet implemented. This is what fleet-api will provide.

These are the same pattern at different scales. The transport differs (terminal, in-process, HTTP), but the lifecycle is identical. Without formalizing this, each implementation will diverge — different semantics for "interrupt," different streaming contracts, different authority models.

This RFC names the pattern so that RFC 1 can implement it and RFC 2 can recurse it.

---

## 2. Definition

A **Principal Orchestrator** is any entity (human or agent) that holds full lifecycle authority over a task. The authority includes six operations that form a closed set:

| # | Operation | What it does | When it's used |
|---|-----------|-------------|----------------|
| 1 | **Create** | Define a task with intent, constraints, and deliverables | Starting work |
| 2 | **Monitor** | Observe execution in real time via streaming | During work |
| 3 | **Interrupt** | Pause, redirect, or terminate execution | When course correction is needed |
| 4 | **Context** | Inject typed information into a running task | When new information arrives |
| 5 | **Review** | Inspect completed or in-progress output | At milestones or completion |
| 6 | **Retask** | Refine and relaunch based on review, preserving lineage | When output needs adjustment |

These six operations are **necessary and sufficient** for task lifecycle control. Any interaction between a principal and an executor can be decomposed into a sequence of these operations.

**Completeness test**: The following edge cases were verified during review:

- "Discover available capabilities" — Not a task lifecycle operation. This is a **discovery** concern, handled by `/manifest` and `GET /workflows` in RFC 1.
- "Delegate authority to another monitor" — **Authority transfer**, a governance concern outside the lifecycle set. If needed later, it extends the authority model, not the operation set.
- "The task errored" — Covered by Monitor (streaming error events) and Review (inspecting the failed state). No missing operation.
- "Cancel a paused task" — Covered. Interrupt handles all three modes, and pause-to-cancel is a valid transition.

---

## 3. Authority Chain

Authority delegates downward. Each level is a Principal Orchestrator to the level below:

```
Dan (Human Principal)
  +-- Sage (Agent Orchestrator)
  |     +-- Local subagents (TaskCreate/Task)
  |     +-- Remote agents (fleet-api)
  |     |     +-- Pi.dev squads
  |     |           +-- Squad orchestrator
  |     |           |     +-- Rules/Skills agent
  |     |           |     +-- Task specialist(s)
  |     |           |     +-- GitOps agent
  |     |           +-- ...more squads
  |     +-- Swarm peers (Nexus, Kelvin, Axiom)
  |
  Other Human Principals
  +-- Their orchestrators (same pattern)
```

### 3.1 Singular Authority

Authority over a task is **singular**. One principal per task. Multiple principals create conflicting interrupts and context injections — the protocol cannot resolve contradictory instructions from co-equal authorities.

The chain model handles multi-party involvement: Dan monitors through Sage, Sage monitors through fleet-api, fleet-api monitors through the squad. At each level, one principal holds write authority (interrupt, context, retask). Observation may be shared — multiple entities can read the SSE stream — but only the designated principal may write.

If Dan needs to interrupt a squad task, he interrupts Sage, who interrupts fleet-api, who interrupts the squad. The chain propagates; it does not fork.

### 3.2 Shared Execution Endpoints

Multiple agents can invoke the same workflow endpoint, each running and managing their own independent task. Authority is per-task, not per-endpoint. A pi.dev workflow might have three concurrent callers — each is the sole principal of its own task, with no cross-task authority.

### 3.3 Swarm Peers

Nexus, Kelvin, and Axiom are not subordinate — they are peers with their own principals. Sage coordinates via swarm messages (request/response), not lifecycle control. The Principal Orchestrator pattern applies to **directed work**, not peer collaboration.

### 3.4 Delegation Depth

**Practical limit: 4 levels.** Human, Agent, Fleet-api, Squad, Specialist. Beyond this, interrupt and context propagation latency makes real-time control impractical.

The pattern itself is unbounded — recursion holds at any depth. But implementations must enforce a ceiling. Every task carries a `delegation_depth` counter. Each delegation increments it. At the configured maximum, the executor must execute directly or fail.

RFC 1 implements this via `FLEET_DELEGATION_MAX_DEPTH` (default: 4).

---

## 4. Operation Semantics

### 4.1 Create

The principal defines:

- **Intent**: What should be accomplished (not how).
- **Constraints**: Boundaries, standards, non-negotiables.
- **Deliverables**: What "done" looks like.
- **Authority scope**: What the executor may decide autonomously vs. what requires escalation.

The executor acknowledges with a plan or begins immediately, depending on complexity.

### 4.2 Monitor

Real-time observation of execution. The contract:

- **Streaming**: The executor emits progress as it works via structured events.
- **Non-blocking**: Monitoring does not pause execution.
- **Selective**: The principal can monitor continuously or check periodically.
- **Typed events**: Stream events carry types — `status`, `progress`, `log`, `milestone`, `decision`, `error`, `context_injected`, `escalation`, `completed`, `heartbeat`.
- **Read-only**: Monitoring is observation. Write operations (interrupt, context, retask) use separate channels.

#### 4.2.1 Executor-to-Principal Signaling (Reverse Channel)

The six operations are principal-to-executor (top-down). But the Monitor stream also carries **executor-initiated signals** in the reverse direction:

| Signal | Meaning |
|--------|---------|
| `escalation` | "I cannot proceed without a decision from you." |
| `clarification_needed` | "The instructions are ambiguous — which interpretation?" |
| `authority_exceeded` | "This action is outside my defined scope." |
| `resource_warning` | "Approaching limits (context window, rate limit, timeout)." |

These are not a seventh operation. They are the return path on the Monitor channel. The principal receives them as SSE events and responds via Context injection or Interrupt.

This is what separates the Principal Orchestrator pattern from a fire-and-forget queue: the executor can communicate back without the principal polling for it.

### 4.3 Interrupt

The principal redirects or pauses execution. Three modes:

| Mode | Semantics | Executor response |
|------|-----------|-------------------|
| **Pause** | "Hold here until I say continue." | Preserve state. Emit `paused` event. Wait for resume or cancel. |
| **Resume** | "Continue from where you stopped." | Reconstruct from saved state. Emit `running` event. |
| **Cancel** | "Stop completely." | Terminate, report final state, clean up. Emit `cancelled` event. |

Interrupt is **graceful** — the executor has a window to save state, report what was accomplished, and clean up. Hard kills are a last resort.

**Redirect** is a compound operation: cancel the current task with a `redirected` terminal status, then create a new task with adjusted constraints that inherits context from the original. RFC 1 provides a dedicated `/redirect` endpoint that performs this atomically.

#### 4.3.1 Pause State Ownership

When a task is paused, **state lives with the executor**. The principal holds a handle (task ID + status), not execution internals. The executor is responsible for snapshotting its own state. On resume, the principal sends the handle; the executor reconstructs from its own snapshot.

This mirrors how the team already works: when Sage compacts, Sage manages state via agent-memory. Dan does not store Sage's context.

#### 4.3.2 Pause TTL

Paused tasks auto-cancel after a configurable timeout (default: implementation-defined). This prevents resource leaks from abandoned tasks. The operational guardrail is necessary because the pattern alone does not solve resource management.

### 4.4 Context

New information injected into a running task. The executor integrates it without restarting.

Context injection is what separates a Principal Orchestrator from a fire-and-forget job queue. **Tasks are living conversations, not batch jobs.**

#### 4.4.1 Typed Context

Context injections carry a type that tells the executor HOW to incorporate the information, not just THAT information arrived:

| Type | Meaning | Example |
|------|---------|---------|
| `additional_input` | New data to consider | "Also check alignment with the Agentic API Standard." |
| `constraint` | A new boundary to respect | "Don't modify files in src/core/." |
| `correction` | Fix something in progress | "The API key format is wrong — use base64, not hex." |
| `reference` | Supplementary material | "Here's the schema from RFC 1 for cross-reference." |

The type is guidance, not a hard constraint. The executor decides how integration affects current work.

#### 4.4.2 Ordering and Conflict Resolution

Context injections are **ordered and sequenced**. Each injection carries a sequence number. If the executor is mid-operation, the injection is queued and applied at the next safe checkpoint. No merge strategy is needed — context injections are additive and ordered.

If injections ARE contradictory, that is a **principal error** (sending conflicting instructions), not a protocol problem. The protocol guarantees ordering, not coherence.

#### 4.4.3 Delivery Acknowledgment

When the executor processes a context injection, it emits a `context_injected` SSE event with the sequence number. The principal confirms the executor received and integrated the context. Without this, context injection would be fire-and-forget, violating the "tasks are conversations" principle.

### 4.5 Review

The principal inspects output. Two modes:

- **Milestone review**: Checkpoint during execution. "Show me what you have so far."
- **Completion review**: Final output inspection. "Here's the deliverable."

Review produces one of three outcomes:

| Outcome | Meaning | Next action |
|---------|---------|-------------|
| **Accept** | Work is done. | Task closes. |
| **Adjust** | Minor corrections needed. | Context injection or retask. |
| **Reject** | Fundamental issue. | Cancel and retask from scratch. |

### 4.6 Retask

Refinement cycle. The principal takes review output and launches a new iteration:

- Same intent, adjusted constraints.
- "Good, but change X and Y."
- Carries forward context from the previous iteration — the executor knows what was tried.

**Retask is not a new task — it is a continuation.** The executor inherits the history.

#### 4.6.1 Retask Lineage

Each retask creates a linked chain, not a replacement. The lineage includes:

- **`parent_task_id`**: The task being refined.
- **`root_task_id`**: The original task that started the chain.
- **`depth`**: How many iterations deep.
- **`chain`**: Ordered list of all task IDs in the lineage.

Retask depth is limited (default: implementation-defined) to prevent infinite refinement loops. RFC 1 implements this via `FLEET_RETASK_MAX_DEPTH`.

---

## 5. Mapping to Implementations

### 5.1 Human to Agent (Dan to Sage)

| Operation | Current implementation |
|-----------|-----------------------|
| Create | Dan types a message in the terminal |
| Monitor | Dan reads Sage's streaming output |
| Interrupt | Dan types while Sage is working / sends correction |
| Context | "Wake: new message from kelvin" / mid-session directive |
| Review | Dan reads the output, responds |
| Retask | "Good, but change X" / "Try again with Y" |

### 5.2 Agent to Local Subagent (Sage to specialist)

| Operation | Current implementation |
|-----------|-----------------------|
| Create | TaskCreate (planning) + Task with `run_in_background` (execution) |
| Monitor | TaskGet / TaskOutput |
| Interrupt | TaskStop |
| Context | **Not supported** (limitation — subagents cannot receive mid-task context) |
| Review | Read TaskOutput after completion |
| Retask | New Task call with adjusted prompt |

**Gap identified**: Local subagents cannot receive context mid-execution. This is a limitation of the current Task tool, not the pattern. Fleet-api solves this.

### 5.3 Agent to Remote Agent (fleet-api)

| Operation | RFC 1 implementation |
|-----------|---------------------|
| Create | `POST /workflows/{id}/run` |
| Monitor | `GET /workflows/{id}/tasks/{task_id}/stream` (SSE) |
| Interrupt | `POST .../tasks/{task_id}/{pause\|resume\|cancel\|redirect}` |
| Context | `POST .../tasks/{task_id}/context` (typed, sequenced) |
| Review | `GET .../tasks/{task_id}` (state-dependent HATEOAS links) |
| Retask | `POST .../tasks/{task_id}/retask` (with lineage tracking) |

RFC 1 splits Interrupt into separate endpoints per mode — clearer API surface than a mode parameter. This is the right implementation decision; the conceptual model groups them as one operation.

---

## 6. Worked Example: Full Lifecycle

This is a concrete example showing how the six operations compose into a real interaction. This example is the actual review process that produced this document.

```
LEVEL: Human (Dan) --> Agent (Sage)

1. CREATE
   Dan: "Review the RFC series and post comments."
   --> Sage begins. Intent clear. Constraints implicit (team standards).
       Deliverable: posted review comments on Issues #1-#4.

2. MONITOR
   Dan reads Sage's streaming output as it drafts comments.
   Sage emits progress: reading RFC 0... drafting review... posting...

3. CONTEXT (additional_input)
   Dan: "Also check alignment with the Agentic API Standard."
   --> Sage integrates the new constraint without restarting.
       Emits context_injected.

4. MONITOR (continued)
   Dan sees Sage incorporating the standard into its review.
   Sage emits milestone: "RFC 0 review posted."

5. REVIEW (milestone)
   Dan reads the posted comment on Issue #3.
   Outcome: ADJUST — "Good, but also address multi-agent concurrency."

6. CONTEXT (correction)
   Dan: "Multiple agents can call the same pi.dev endpoint,
         each running and managing their own task."
   --> Sage integrates. Emits context_injected.

7. REVIEW (completion)
   Dan reads all posted comments.
   Outcome: ACCEPT with follow-up.

8. RETASK
   Dan: "Let's move this into a synthesized markdown.
         Structure it well, use our standard for zero cognitive debt."
   --> New task inherits full context from the review cycle.
       Sage (via Nexus) knows what was tried, what was reviewed,
       what feedback was given. This document is the retask output.
```

**The same sequence at the fleet-api level:**

```
LEVEL: Agent (Sage) --> Remote Agent (pi.dev squad) via fleet-api

1. CREATE   POST /workflows/code-review/run
             { intent: "Review RFC series", constraints: {...} }
             --> 202 Accepted, task_id: "t-abc123"

2. MONITOR  GET  /workflows/code-review/tasks/t-abc123/stream
             --> SSE: {type: "progress", data: "Reading RFC 0..."}
             --> SSE: {type: "milestone", data: "RFC 0 review drafted"}

3. CONTEXT  POST /workflows/code-review/tasks/t-abc123/context
             { context_type: "additional_input", sequence: 1,
               message: "Also check Agentic API Standard alignment." }
             --> SSE: {type: "context_injected", sequence: 1}

4. MONITOR  --> SSE: {type: "progress", data: "Adding standard checks..."}
             --> SSE: {type: "milestone", data: "All reviews posted"}

5. REVIEW   GET  /workflows/code-review/tasks/t-abc123
             --> { status: "completed", result: {...}, _links: {retask: ...} }

6. RETASK   POST /workflows/code-review/tasks/t-abc123/retask
             { adjustments: "Synthesize into a single markdown doc.",
               constraints: { style: "zero cognitive debt" } }
             --> 202 Accepted, task_id: "t-def456"
                 lineage: { parent: "t-abc123", root: "t-abc123", depth: 1 }
```

The operations are identical. The transport changed from terminal to HTTP. The lifecycle did not.

---

## 7. Design Principles

1. **The interface is recursive.** Every level uses the same six operations. Don't invent new verbs.
2. **Tasks are conversations, not jobs.** Context injection and retasking are first-class. Fire-and-forget is a degenerate case.
3. **Streaming is mandatory.** A task that can't be monitored can't be interrupted or contextualized. Silent execution is a black box.
4. **Authority is explicit and singular.** Every task defines what the executor may decide alone. One principal per task. Observation may be shared; control may not.
5. **The transport is irrelevant.** Terminal, in-process, HTTP, swarm — the six operations apply regardless. Implementations should feel identical to the principal. Transport adapters (such as the sidecar in RFC 2) may differ per level; the semantic model does not.
6. **Context is typed.** The executor needs to know HOW to incorporate new information, not just that it arrived. Types (`additional_input`, `constraint`, `correction`, `reference`) are guidance at the pattern level; implementations may extend the set.
7. **The channel is bidirectional.** The principal sends operations down; the executor sends signals up via the Monitor stream. Escalation, clarification, and resource warnings flow on the return path.

---

## 8. RFC 1 Feedback Loop

RFC 1 (Agentic Task API) implemented this pattern and discovered improvements that feed back into shared understanding. This is the intended design process: RFC 0 provides the conceptual model; implementation discovers details.

| RFC 0 Concept | RFC 1 Implementation | What it taught us |
|---|---|---|
| Interrupt (3 modes in one operation) | Separate `/pause`, `/resume`, `/cancel`, `/redirect` endpoints | Each mode is independently callable. Clearer API surface. |
| Context (untyped "new information") | `context_type` enum with 4 values | Executor needs to know HOW to integrate, not just THAT context arrived. Elevated to pattern level (Section 4.4.1). |
| Retask (carries forward context) | `parent_task_id` + `lineage` object + chain depth limit | Retask creates a linked chain, not a replacement. Overflow protection is necessary. |
| Monitor (streaming) | SSE with typed events: `status`, `progress`, `log`, `context_injected`, `completed`, `failed`, `heartbeat` | Structured stream is essential. Raw text is insufficient for programmatic principals. |
| Review (inspect output) | State-dependent HATEOAS `_links` | The API itself communicates which operations are valid for the current state. |
| (not specified) | Pause state TTL with auto-cancellation | The pattern alone does not solve resource management. Operational guardrails are an implementation responsibility. |

---

## 9. Resolved Design Decisions

These were open questions during drafting. Resolved by team consensus (Kelvin, Sage, Nexus — positions documented in Issue #3 comments).

### 9.1 Authentication Model

**Decision: Ed25519 signatures reusing ASP swarm keypairs.**

Request signing over METHOD + PATH + TIMESTAMP + BODY_HASH with a 5-minute replay window. No JWT. The swarm already has a working Ed25519 identity system. Adding a second authentication mechanism creates cognitive debt for zero benefit.

Resolved by: Sage (OQ #1 response), confirmed by Nexus (review Section G).

### 9.2 Multi-Principal

**Decision: Authority is singular per task.**

Multiple principals create conflicting interrupts. The chain model handles multi-party involvement: Dan monitors through Sage, not alongside Sage. Observation (read) may be shared; authority (write — interrupt, context, retask) is singular.

Resolved by: Kelvin (initial position), Sage (confirmed with observation nuance), Nexus (confirmed).

### 9.3 Delegation Depth

**Decision: Practical limit of 4 levels (Human, Agent, Fleet, Squad, Specialist).**

The pattern itself is unbounded, but each level adds latency to interrupt and context propagation. Beyond 4 levels, real-time control becomes impractical. Implementations enforce via `delegation_depth` counter.

Resolved by: Kelvin (initial position with latency rationale), Sage (agreed, added enforcement mechanism), Nexus (agreed, noted as recommendation not hard constraint in pattern).

### 9.4 State Persistence on Interrupt

**Decision: State lives with the executor.**

The principal holds a handle (task ID + status). The executor stores execution state and is responsible for snapshotting on pause. On resume, the principal sends the handle; the executor reconstructs. Fleet-api stores the handle and metadata, not the execution state.

Resolved by: Kelvin (initial position), Sage (confirmed), Nexus (confirmed with agent-memory analogy).

### 9.5 Context Conflict Resolution

**Decision: Ordered queue with delivery acknowledgment.**

Context injections are sequenced and additive. Applied in order at safe checkpoints. Contradictory instructions are a principal error, not a protocol problem. The protocol guarantees ordering, not coherence.

Addition: Each processed injection emits a `context_injected` SSE event with its sequence number, confirming delivery and integration.

Resolved by: Kelvin (queue with sequence numbers), Sage (added delivery acknowledgment), Nexus (confirmed).

---

## 10. Compliance

This RFC establishes the interaction model. Implementations must demonstrate:

1. **All six operations are supported.** No operation may be omitted or collapsed into another.
2. **The recursive property holds.** At the implementation's level of the authority chain, the same six operations apply.
3. **Executor-to-principal signaling exists.** The Monitor channel carries return signals (escalation, clarification, authority-exceeded).
4. **Context is typed.** Implementations must support at minimum the four context types defined in Section 4.4.1.
5. **Retask preserves lineage.** The chain of refinements is traceable from any task back to its root.
6. **Gold Tier target.** Fleet-api implementations target Agentic API Standard Gold Tier (all 20 patterns). RFC 1 specifies how each pattern is fulfilled.

### 10.1 RFC Cross-References

| RFC | Relationship to RFC 0 |
|-----|----------------------|
| **RFC 1** (Agentic Task API) | Implements the six operations as HTTP endpoints. Adds operational concerns (rate limits, health, workflow registry). Proves the pattern works at the Agent-to-Remote-Agent level. |
| **RFC 2** (Pi.dev Sub-Agent Architecture) | Applies the recursion inside remote squads. The squad orchestrator is a Principal Orchestrator to its specialists. Adds the sidecar as transport adapter. Proves the pattern works at the Squad-to-Specialist level. |

---

## 11. Provenance

This document synthesizes the original RFC 0 draft (Sage, Issue #3 on `nexus-marbell/fleet-api`) with review comments from the full team:

- **Kelvin** (mlops-kelvin): Positions on OQ #2-5, delegation depth rationale.
- **Sage** (finml-sage): Author response closing all OQs, delivery acknowledgment addition.
- **Nexus** (nexus-marbell): 8-recommendation structured review — completeness verification, recursion analysis, RFC 1 alignment table, cognitive debt assessment.
- **Dan** (vantasnerdan): Multi-agent concurrency point, directive to synthesize.
- **Axiom** (axiom-marbell): Shadow review — Gold Tier gap analysis, cognitive load metric.

The Principal Orchestrator pattern is not new — Dan has been using it since session one. This RFC names what already works and extends it across machines.
