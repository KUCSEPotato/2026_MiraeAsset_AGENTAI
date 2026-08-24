from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.agent.service import get_answer_service
from app.api.answer import router as answer_router


@asynccontextmanager
async def lifespan(application: FastAPI):
    del application
    service = get_answer_service()
    try:
        yield
    finally:
        close = getattr(service, "close", None)
        if close is not None:
            await close()
        get_answer_service.cache_clear()


def create_app() -> FastAPI:
    application = FastAPI(
        title="Financial Semantic Agent",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.include_router(answer_router)

    @application.get("/health", tags=["operations"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return application


app = create_app()
