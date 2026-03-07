# Fleet API

**Distributed task dispatch for federated agent fleets.**

Fleet API is how autonomous agents do work for each other across machines, models, and platforms. It is the tasking layer for the [Agent Swarm Protocol](https://github.com/finml-sage/agent-swarm-protocol) — ASP handles messaging ("talk to each other"), Fleet API handles tasking ("do work for each other").

## Why This Exists

Agents today are trapped in single-session silos. An orchestrator on one VM cannot invoke a specialist on another. A Claude agent cannot delegate reasoning to a Grok agent. A fleet of agents cannot discover what each other can do without out-of-band knowledge.

Fleet API solves this with three primitives:
- **Workflows**: Registered capabilities that any authenticated agent can discover and invoke
- **Tasks**: Lifecycle-managed work units with real-time streaming, pause/resume, and lineage tracking
- **Agents**: Authenticated identities (Ed25519) that register as providers or act as callers

## What Makes It Different

**Model-agnostic by design.** Any system that speaks HTTP and Ed25519 can participate. Our fleet runs Claude, Grok 4.20, and custom models side by side. The API doesn't care what's behind the endpoint — it cares that the contract is honored.

**Tasks are conversations, not jobs.** Most task systems are fire-and-forget: submit work, poll for results. Fleet API implements the [Principal Orchestrator Pattern](docs/rfc-0-principal-orchestrator-pattern.md) — six operations (Create, Monitor, Interrupt, Context, Review, Retask) that give the caller full lifecycle authority. You can pause a running task, inject new context mid-execution, redirect it to a different agent, or retask with refinement while preserving the full lineage chain. This is how humans actually direct work. Now agents can too.

**Recursive at every level.** The same six operations apply whether a human is directing an agent, an agent is directing a subagent, or a squad orchestrator is directing specialists. The pattern is fractal — a principal at one level is an executor at the level above.

**Pull-based dispatch with zero cognitive debt.** Agents poll for work through a sidecar that handles registration, heartbeat, and task execution. The sidecar is a single Docker image — configure it with an environment variable (`FLEET_EXECUTOR_COMMAND`) pointing at your agent's entrypoint. No SDK to integrate, no protocol to implement. If your agent can accept a JSON input on stdin and write results to stdout, it can join the fleet.

**Built to the [Agentic API Standard](https://github.com/nexus-marbell/agentic-api-standard).** Self-describing manifest, HATEOAS navigation in every response, structured errors with suggestions and recovery links, onboarding steps after registration, health-gated workflow listing. A naive agent can discover the API surface, register, and start accepting work without reading any documentation.

## What This Is NOT

- **Not an orchestrator.** Fleet API is the directory and dispatch layer. Orchestration logic stays in each agent. Your agent decides what to do — Fleet API gives it the ability to ask other agents to do things.
- **Not a replacement for ASP.** The Agent Swarm Protocol handles swarm membership, peer-to-peer messaging, and identity. Fleet API handles structured task delegation. They share the same Ed25519 keypairs.
- **Not a queue.** There is no broker, no consumer groups, no dead letter queue. Tasks are dispatched to specific workflows owned by specific agents. The sidecar polls and executes. If the agent is unreachable, the heartbeat monitor marks it and the fleet knows.

## Architecture

```
Fleet API (centralized, Dokploy)
  |
  +-- GET  /manifest              API discovery + auth info
  +-- POST /agents/register       Ed25519 identity bootstrap
  +-- GET  /workflows             Capability registry (health-gated)
  +-- POST /workflows/{id}/run    Task dispatch
  +-- GET  /tasks/{id}            Task state + streaming
  +-- GET  /health                Operational status
  |
  +--- HTTPS (Ed25519 signed requests) ---+
  |                                        |
  Agent VMs (federated)                 Remote Squads
  +-- Fleet Sidecar (Docker)            +-- Squad Orchestrator
  +-- FLEET_EXECUTOR_COMMAND            +-- Specialists
  +-- Heartbeat + Auto-registration     +-- Fleet Sidecar
```

## Current Status

**Phase 1 + 1.5: Complete.** 18 PRs merged, 485 tests, zero cognitive debt carried forward.

What's implemented:
- Agent registration with Ed25519 public key authentication
- Workflow registration, discovery, and health-gated listing
- Full task lifecycle: create, accept, run, complete, fail, cancel
- Task state machine with enforced transitions (8 states, validated)
- Sidecar with self-registration, heartbeat loop, and configurable executor
- Heartbeat timeout monitoring (marks agents UNREACHABLE after 90s)
- Containerized deployment (separate API and sidecar Dockerfiles)
- Pull-based task dispatch with exponential backoff
- HATEOAS navigation, structured errors, onboarding flow
- Manifest endpoint with full API surface description

**Phase 2: In Progress.** Real-time SSE streaming, pause/resume, context injection, retask with lineage, callback signing, executor-to-principal signaling.

**Phase 3: Planned.** Rate limiting, near-miss path matching, WebMCP, schema versioning, idempotency keys.

## Quick Start

```bash
# Clone
git clone https://github.com/nexus-marbell/fleet-api.git
cd fleet-api

# Install
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Run
uvicorn fleet_api.app:create_app --factory --port 8000

# Test
pytest -v
```

## Docker

```bash
# Start Fleet API + PostgreSQL
docker compose up

# Fleet API at http://localhost:8000
# Try: curl http://localhost:8000/manifest
```

## Run a Sidecar

```bash
# Build the sidecar image
docker build -f Dockerfile.sidecar -t fleet-sidecar .

# Run with your agent's command
docker run -e FLEET_API_URL=http://fleet-api:8000 \
           -e FLEET_AGENT_ID=my-agent \
           -e FLEET_AGENT_PRIVATE_KEY_PATH=/keys/private.pem \
           -e FLEET_EXECUTOR_COMMAND="python -m my_agent" \
           fleet-sidecar
```

For custom dependencies, extend the base image:

```dockerfile
FROM fleet-sidecar:latest
RUN pip install my-custom-package
ENV FLEET_EXECUTOR_COMMAND="python -m my_agent"
```

## RFCs

| RFC | Title | Description |
|-----|-------|-------------|
| [RFC 0](docs/rfc-0-principal-orchestrator-pattern.md) | Principal Orchestrator Pattern | The foundational pattern: 6 operations, recursive at every level. Tasks are conversations, not jobs. |
| [RFC 1](docs/rfc-1-agentic-task-api.md) | Agentic Task API | HTTP implementation of RFC 0. Full endpoint spec, SSE streaming, pull-based dispatch. |
| [RFC 2](docs/rfc-2-sub-agent-architecture.md) | Sub-Agent Architecture | Multi-agent squad model for remote execution. Proves RFC 0 recursion at the squad level. |

## Project Structure

```
fleet-api/
  src/
    fleet_api/              # Central API server
      agents/               # Registration, auth, heartbeat monitoring
      workflows/            # Capability registry and dispatch
      tasks/                # Task lifecycle, state machine (8 states)
      middleware/            # Ed25519 auth, error handling
      database/             # SQLAlchemy async, migrations
      app.py                # FastAPI factory with lifespan
    fleet_agent/            # Sidecar (polling, dispatch, heartbeat)
  tests/                    # 485 tests
  docs/                     # RFCs
  alembic/                  # Database migrations
```

## Compliance

Fleet API targets [Agentic API Standard](https://github.com/nexus-marbell/agentic-api-standard) Gold Tier — all 20 design patterns for self-describing, machine-first interfaces.

## License

MIT
