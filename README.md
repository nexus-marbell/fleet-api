# Fleet API

Distributed task dispatch and workflow registry for federated agent fleets.

Fleet API is the tasking layer for the [Agent Swarm Protocol](https://github.com/finml-sage/agent-swarm-protocol). ASP handles messaging ("talk to each other"). Fleet API handles tasking ("do work for each other").

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

## Development Setup

```bash
# Create virtual environment
python -m venv .venv
source .venv/bin/activate

# Install with dev dependencies
pip install -e ".[dev]"

# Run linter
ruff check src/ tests/

# Run type checker
mypy src/

# Run tests
pytest -v

# Run with coverage
coverage run -m pytest -v
coverage report
```

## Docker

```bash
# Start Fleet API + PostgreSQL
docker compose up

# Fleet API available at http://localhost:8000
# PostgreSQL available at localhost:5432
```

## Configuration

Copy `.env.example` to `.env` and adjust values:

```bash
cp .env.example .env
```

See `.env.example` for all available configuration variables.

## Project Structure

```
fleet-api/
  src/
    fleet_api/              # Main API package
      agents/               # Agent registration, auth, lifecycle
      workflows/            # Capability registry and dispatch
      tasks/                # Task lifecycle, state machine
      middleware/            # Auth (Ed25519), error handling
      database/             # SQLAlchemy async engine, base model
      app.py                # FastAPI application factory
      config.py             # Pydantic settings
    fleet_agent/            # Sidecar agent (task polling, dispatch)
  tests/                    # pytest test suite
  alembic/                  # Database migrations
  docs/                     # RFCs and design documents
```

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
