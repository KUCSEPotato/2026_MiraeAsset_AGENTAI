from fastapi import FastAPI

from app.api.answer import router as answer_router


def create_app() -> FastAPI:
    application = FastAPI(
        title="Financial Semantic Agent",
        version="0.1.0",
    )
    application.include_router(answer_router)

    @application.get("/health", tags=["operations"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return application


app = create_app()

