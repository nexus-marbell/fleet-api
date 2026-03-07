"""FastAPI application factory."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import Depends, FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from fleet_api.agents.heartbeat_monitor import heartbeat_monitor
from fleet_api.agents.routes import router as agents_router
from fleet_api.agents.service import DatabaseAgentLookup
from fleet_api.config import settings
from fleet_api.database.connection import async_session, get_session
from fleet_api.health import health_router
from fleet_api.manifest import router as manifest_router
from fleet_api.middleware.auth import AgentLookup, get_agent_lookup
from fleet_api.middleware.errors import register_error_handlers
from fleet_api.tasks.routes import router as tasks_router
from fleet_api.workflows.routes import router as workflows_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan: startup and shutdown."""
    # Startup: launch heartbeat monitor background task
    monitor_task = asyncio.create_task(
        heartbeat_monitor(
            async_session,
            timeout_seconds=settings.fleet_heartbeat_timeout_seconds,
            sweep_interval=settings.fleet_heartbeat_sweep_interval,
        )
    )
    yield
    # Shutdown: cancel the heartbeat monitor
    monitor_task.cancel()
    with suppress(asyncio.CancelledError):
        await monitor_task


async def _get_database_agent_lookup(
    session: AsyncSession = Depends(get_session),
) -> AgentLookup:
    """Provide a real DatabaseAgentLookup backed by the database session."""
    return DatabaseAgentLookup(session)


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="Fleet API",
        description="Distributed task dispatch for agentic workflows",
        version="0.1.0",
        lifespan=lifespan,
    )

    register_error_handlers(app)

    # Wire the real agent lookup into the auth middleware
    app.dependency_overrides[get_agent_lookup] = _get_database_agent_lookup

    app.include_router(health_router)
    app.include_router(manifest_router)
    app.include_router(agents_router, prefix="/agents", tags=["agents"])
    app.include_router(workflows_router, prefix="/workflows", tags=["workflows"])
    app.include_router(tasks_router)

    @app.get("/")
    async def root() -> dict[str, str]:
        return {"status": "ok"}

    return app
