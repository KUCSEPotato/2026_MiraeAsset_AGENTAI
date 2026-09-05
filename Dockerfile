# syntax=docker/dockerfile:1.7

FROM ghcr.io/astral-sh/uv:0.8.14 AS uv

FROM python:3.12.11-slim-bookworm AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app

COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

FROM python:3.12.11-slim-bookworm AS runtime

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --gid 10001 agent \
    && useradd --uid 10001 --gid agent --no-create-home --shell /usr/sbin/nologin agent

WORKDIR /app
COPY --from=builder --chown=agent:agent /app/.venv /app/.venv
COPY --chown=agent:agent app /app/app
COPY --chown=agent:agent ontology /app/ontology
COPY --chown=agent:agent alembic /app/alembic
COPY --chown=agent:agent alembic.ini pyproject.toml README.md /app/

USER 10001:10001
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]
