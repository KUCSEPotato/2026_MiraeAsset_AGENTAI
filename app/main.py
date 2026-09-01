from contextlib import asynccontextmanager
import os
import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.agent.service import get_answer_service
from app.api.answer import router as answer_router
from app.data.database import DATABASE_BACKEND, DATABASE_SCHEMA_VERSION
from app.data.v2_schema import CANONICAL_V2_SCHEMA_VERSION
from app.ontology.runtime_mapping import SEMANTIC_MAPPING_VERSION
from app.operations import OperationalSettings, configure_logging

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
FRONTEND_INDEX = FRONTEND_DIR / "index.html"


@asynccontextmanager
async def lifespan(application: FastAPI):
    service_provider = application.dependency_overrides.get(
        get_answer_service, get_answer_service
    )
    service = service_provider()
    try:
        validate_derived_stores = getattr(service, "validate_derived_stores", None)
        if validate_derived_stores is not None:
            await validate_derived_stores()
        runtime_health = getattr(service, "runtime_health", None)
        if runtime_health is not None:
            application.state.runtime_health = runtime_health()
        application.state.ready = True
        yield
    finally:
        application.state.ready = False
        close = getattr(service, "close", None)
        if close is not None:
            await close()
        cache_clear = getattr(get_answer_service, "cache_clear", None)
        if cache_clear is not None:
            cache_clear()


def create_app() -> FastAPI:
    operational_settings = OperationalSettings.from_env()
    configure_logging(operational_settings)
    application = FastAPI(
        title="Financial Semantic Agent",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.state.operational_settings = operational_settings
    application.state.ready = False
    application.include_router(answer_router)
    if FRONTEND_INDEX.exists():
        application.mount(
            "/assets",
            StaticFiles(directory=FRONTEND_DIR / "assets"),
            name="frontend-assets",
        )

    @application.exception_handler(Exception)
    async def controlled_internal_error(
        request: Request, exc: Exception
    ) -> JSONResponse:
        logging.getLogger(__name__).error(
            "unhandled API error",
            extra={
                "error_class": type(exc).__name__,
                "http_status": 500,
                "request_id": request.headers.get("x-request-id", "unavailable"),
            },
        )
        return JSONResponse(status_code=500, content={"detail": "internal service error"})

    @application.get("/live", tags=["operations"])
    async def live() -> dict[str, str]:
        return {"status": "alive"}

    @application.get("/health", tags=["operations"])
    async def health():
        runtime = getattr(application.state, "runtime_health", {})
        ontology_version = runtime.get("ontology_version", os.getenv("ONTOLOGY_VERSION", "team-v1"))
        bundle = runtime.get("active_runtime_bundle", "canonical_v1")
        repository_version = "v2" if bundle == "canonical_v2" else "v1"
        # A route cannot be served before lifespan yields. Runtime compatibility
        # is therefore the authoritative readiness signal, including in
        # test-owned lifespan contexts.
        ready = runtime.get("compatibility_status", "NOT_READY") == "READY"
        base = {
            "status": "ok" if ready else "not_ready",
            "process_status": "alive",
            "readiness_status": "READY" if ready else "NOT_READY",
            "database_backend": DATABASE_BACKEND,
            "database_schema_version": (
                CANONICAL_V2_SCHEMA_VERSION
                if repository_version == "v2"
                else DATABASE_SCHEMA_VERSION
            ),
            "dataset_generation": "260824",
            "dataset_snapshot": os.getenv(
                "DATA_SNAPSHOT_DATE", "2026-08-24"
            ),
            "ontology_version": ontology_version,
            "semantic_mapping_version": (
                SEMANTIC_MAPPING_VERSION
                if ontology_version in {"team-v1", "team_v1", "merged-optical-1.3", "merged-optical-1.4"}
                else "legacy-runtime-v0"
            ),
            "graph_version": os.getenv(
                "GRAPH_VERSION", "m10.7-team-v1-20260829"
            ),
        }
        if "graph_projection_version" in runtime:
            base["graph_version"] = runtime["graph_projection_version"]
        payload = {**base, **runtime}
        return JSONResponse(status_code=200 if ready else 503, content=payload)

    if FRONTEND_INDEX.exists():

        @application.get("/", include_in_schema=False)
        async def frontend_landing() -> FileResponse:
            return FileResponse(FRONTEND_INDEX)

        @application.get("/chat", include_in_schema=False)
        async def frontend_chat() -> FileResponse:
            return FileResponse(FRONTEND_INDEX)

    return application


app = create_app()
