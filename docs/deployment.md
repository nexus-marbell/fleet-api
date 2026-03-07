# Deployment Guide — Dokploy

How to deploy fleet-api on [Dokploy](https://dokploy.com/).

## Architecture

Fleet API has three components:

```
                     +-----------------+
    Internet ------->| Traefik (TLS)   |
                     +--------+--------+
                              |
                     +--------v--------+
                     | Fleet API       |  Dockerfile.api
                     | (FastAPI :8000) |
                     +--------+--------+
                              |
                     +--------v--------+
                     | PostgreSQL 16   |  Dokploy-managed
                     +-----------------+

    +-------------------+   +-------------------+
    | Sidecar: agent-1  |   | Sidecar: agent-2  |  Dockerfile.sidecar
    | (:8001 health)    |   | (:8001 health)    |  (one per agent)
    +-------------------+   +-------------------+
```

- **Fleet API server** (`Dockerfile.api`) — FastAPI application, port 8000. Manages agents, tasks, and workflows.
- **Fleet Agent sidecar** (`Dockerfile.sidecar`) — One per agent. Polls the API for tasks, dispatches them to a local executor, streams results back. Port 8001 (health endpoint only).
- **PostgreSQL** — Stores agents, tasks, workflows. Dokploy-managed.

Dokploy deploys each component as a separate service using its Dockerfile. This is NOT a docker-compose deployment.

## Dokploy Service Configuration

### API Service

| Setting | Value |
|---------|-------|
| Source | `Dockerfile.api` from repo root |
| Port | 8000 |
| Health check | `GET /` returns manifest (200 OK) |
| Traefik | TLS termination, route to fleet-api subdomain |

**Environment variables:**

| Variable | Required | Description |
|----------|----------|-------------|
| `DATABASE_URL` | Yes | `postgresql+asyncpg://user:pass@host:5432/fleet_api` |

The `DATABASE_URL` must use the `asyncpg` driver. Point it at your Dokploy-managed PostgreSQL instance.

### Sidecar Service (per agent)

| Setting | Value |
|---------|-------|
| Source | `Dockerfile.sidecar` from repo root |
| Port | 8001 (health endpoint only, not externally exposed) |
| Health check | `GET /fleet/health` on port 8001 |
| Volume | Agent private key PEM file mounted read-only |

**Environment variables:**

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `FLEET_API_URL` | Yes | — | URL of the API service (e.g. `https://fleet.example.com`) |
| `FLEET_AGENT_ID` | Yes | — | Unique agent identifier |
| `FLEET_AGENT_PRIVATE_KEY_PATH` | Yes | — | Path to Ed25519 PEM file inside the container |
| `FLEET_EXECUTOR_COMMAND` | Yes | — | Shell command to run for each task |
| `FLEET_HEARTBEAT_INTERVAL` | No | `30` | Seconds between heartbeats |
| `FLEET_POLL_INTERVAL` | No | `5` | Seconds between task polls |
| `FLEET_MAX_CONCURRENT_TASKS` | No | `1` | Maximum parallel task executions |
| `FLEET_SIDECAR_PORT` | No | `8001` | Health endpoint port |

### Database

Use Dokploy's managed PostgreSQL 16 service. After the database is running, run Alembic migrations from the API container:

```bash
alembic upgrade head
```

The `alembic` binary is included in the `Dockerfile.api` image. You can run it via Dokploy's terminal or as a one-shot command before starting the API.

## Per-Agent Deployment Pattern

The sidecar follows a one-image, many-deployments pattern:

1. **Build once** — A single `Dockerfile.sidecar` image.
2. **Deploy N times** — Each Dokploy deployment gets its own `FLEET_AGENT_ID`, private key, and `FLEET_EXECUTOR_COMMAND`.
3. **Auto-registration** — The sidecar self-registers with the API on startup. No manual agent creation needed.
4. **Lifecycle** — Each sidecar handles its own heartbeat, task polling, and event streaming.

To add a new agent:
1. Generate a key pair (see below).
2. Create a new Dokploy service using `Dockerfile.sidecar`.
3. Set the environment variables with a unique `FLEET_AGENT_ID`.
4. Mount the private key PEM file as a read-only volume.
5. Deploy. The sidecar registers itself and starts polling.

## Key Generation

Each agent needs an Ed25519 key pair. The private key stays with the sidecar; the public key is sent to the API during self-registration.

```bash
# Generate private key
openssl genpkey -algorithm ed25519 -out agent.pem

# Extract public key (for reference/verification)
openssl pkey -in agent.pem -pubout -out agent.pub
```

Mount `agent.pem` into the sidecar container and set `FLEET_AGENT_PRIVATE_KEY_PATH` to its path (e.g. `/keys/agent.pem`).

## Local Development

For local development, use docker-compose:

```bash
docker compose up
```

The `docker-compose.yml` in the repo root starts the API, an example sidecar, and a PostgreSQL instance. This is for **local development only** — production uses Dokploy's per-service deployment.

## Monitoring

| Check | Endpoint | Expected |
|-------|----------|----------|
| API health | `GET /` | 200 with manifest JSON |
| Agent health | `GET /fleet/health` on sidecar port | 200 with agent status |
| Heartbeat timeout | Automatic | Agents marked `UNREACHABLE` after 90s silence |
| Workflow status | `GET /workflows/{id}` | `executor_status` field shows owning agent's health |

The heartbeat monitor runs inside the API server. If an agent's sidecar stops sending heartbeats for 90 seconds, the agent's status transitions to `UNREACHABLE`. The sidecar's heartbeat re-activates the agent automatically when it resumes.
