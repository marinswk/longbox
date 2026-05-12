# syntax=docker/dockerfile:1.7

FROM python:3.13-slim AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /usr/local/bin/

WORKDIR /app

COPY pyproject.toml uv.lock* README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev || uv sync --no-install-project --no-dev

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev


# ---------------------------------------------------------------------------
# Test stage — `docker build --target test -t longbox-test .` builds an image
# with dev deps + the test suite. Kept BEFORE runtime so `runtime` stays the
# default target for `docker compose up --build` and `docker build` without
# `--target`.
# ---------------------------------------------------------------------------
FROM builder AS test

WORKDIR /app
COPY app/tests ./app/tests
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen || uv sync

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

CMD ["pytest", "-q", "app/tests"]


# ---------------------------------------------------------------------------
# Runtime stage — MUST be the last stage in this file so it stays the default
# build target. Anything that builds from this Dockerfile without `--target`
# (compose, plain `docker build .`) lands here.
# ---------------------------------------------------------------------------
FROM python:3.13-slim AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system longbox \
    && useradd --system --gid longbox --home-dir /app --shell /usr/sbin/nologin longbox \
    && mkdir -p /data \
    && chown -R longbox:longbox /data

WORKDIR /app

COPY --from=builder --chown=longbox:longbox /app /app
# Tests live in the source tree but aren't needed at runtime.
RUN rm -rf /app/app/tests

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER longbox
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
