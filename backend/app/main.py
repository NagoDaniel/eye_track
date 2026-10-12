import logging
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import Engine, text

from app.api.admin import router as admin_router
from app.db import make_engine, make_session_factory
from app.logging import configure_logging
from app.settings import Settings

log = logging.getLogger("app.access")

HEALTH_PATH = "/api/health"


def create_app(settings: Settings | None = None, engine: Engine | None = None) -> FastAPI:
    """Build the app. Settings and engine are created at startup unless injected (tests)."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        cfg = settings or Settings()  # type: ignore[call-arg]
        if settings is None:
            configure_logging(cfg.log_level)
        app.state.settings = cfg
        app.state.engine = engine or make_engine(cfg.database_url)
        app.state.session_factory = make_session_factory(app.state.engine)
        try:
            yield
        finally:
            app.state.engine.dispose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def access_log(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        start = time.perf_counter()
        response = await call_next(request)
        # Health is polled every few seconds by the container healthcheck: keep it at DEBUG.
        level = logging.DEBUG if request.url.path == HEALTH_PATH else logging.INFO
        log.log(
            level,
            "request",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": round((time.perf_counter() - start) * 1000, 2),
            },
        )
        return response

    @app.get(HEALTH_PATH)
    def health(request: Request) -> JSONResponse:
        try:
            with request.app.state.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception:
            logging.getLogger("app.health").exception("database check failed")
            return JSONResponse({"status": "unavailable", "error": "database"}, status_code=503)
        return JSONResponse({"status": "ok"})

    app.include_router(admin_router)
    return app


app = create_app()
