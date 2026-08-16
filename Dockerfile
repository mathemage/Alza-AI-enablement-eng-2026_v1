# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:0.12.5 AS uv
FROM python:3.14-slim

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock ./
COPY src ./src

RUN uv sync --locked --no-dev --no-editable --no-cache \
    && useradd --create-home --uid 10001 app

USER app

EXPOSE 8080

CMD ["uvicorn", "alza_ai.main:app", "--host", "0.0.0.0", "--port", "8080"]
