# Fleet API

Distributed task dispatch and workflow registry for federated agent fleets.

Fleet API is the tasking layer for the [Agent Swarm Protocol](https://github.com/finml-sage/agent-swarm-protocol). ASP handles messaging ("talk to each other"). Fleet API handles tasking ("do work for each other").

## RFCs

| RFC | Title | Status | Description |
|-----|-------|--------|-------------|
| [RFC 0](docs/rfc-0-principal-orchestrator-pattern.md) | Principal Orchestrator Pattern | Resolved | Foundational pattern: 6-operation closed set for task lifecycle control. Recursive at every level. |
| [RFC 1](docs/rfc-1-agentic-task-api.md) | Agentic Task API | Resolved | HTTP implementation of RFC 0. Full endpoint spec, SSE streaming, pull-based dispatch, Gold Tier compliance. |
| [RFC 2](docs/rfc-2-sub-agent-architecture.md) | Sub-Agent Architecture | Resolved | Multi-agent squad model for remote execution on pi.dev. Proves RFC 0 recursion at the squad level. |

## Architecture

```
Fleet API (centralized)
  |
  +-- /manifest          Discovery + auth info
  +-- /workflows          Capability registry
  +-- /workflows/{id}/run Task dispatch
  +-- /health             Operational status
  |
  +--- HTTPS (Ed25519 signed) ---+
  |                               |
  Agent VMs (federated)        Pi.dev Squads (remote)
  +-- Fleet Agent Sidecar       +-- Squad Orchestrator
  +-- Local Orchestrator        +-- Specialists
  +-- Specialists               +-- Rules/Skills Agent
                                +-- GitOps Agent
                                +-- Fleet Agent Sidecar
```

## Compliance

Fleet API targets [Agentic API Standard](https://github.com/nexus-marbell/agentic-api-standard) Gold Tier (all 20 patterns).

## Status

Design phase. RFCs 0-2 are resolved (synthesized from team review). Implementation has not started.
