"""FastAPI application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from fleet_api.agents.routes import router as agents_router
from fleet_api.health import health_router
from fleet_api.manifest import router as manifest_router
from fleet_api.middleware.errors import register_error_handlers
from fleet_api.tasks.routes import router as tasks_router
from fleet_api.workflows.routes import router as workflows_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan: startup and shutdown."""
    # Startup: initialize database connection pool
    yield
    # Shutdown: dispose database connection pool


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="Fleet API",
        description="Distributed task dispatch for agentic workflows",
        version="0.1.0",
        lifespan=lifespan,
    )

    register_error_handlers(app)

    app.include_router(health_router)
    app.include_router(manifest_router, tags=["discovery"])
    app.include_router(agents_router, prefix="/agents", tags=["agents"])
    app.include_router(workflows_router, prefix="/workflows", tags=["workflows"])
    app.include_router(tasks_router, prefix="/tasks", tags=["tasks"])

    @app.get("/")
    async def root() -> dict[str, str]:
        return {"status": "ok"}

    return app
