# syntax=docker/dockerfile:1
# Fixed upstream digest; Python 3.13 is required by the locked application.
FROM python:3.13-slim-bookworm@sha256:281d39ff02e3b389401586134ff9e6572954687822a58055ec07a60ded4e0b7f

ARG RERANK_GROUP=rerank
ARG VCS_REF=unknown
LABEL org.opencontainers.image.title="Project Copilot" \
      org.opencontainers.image.revision="$VCS_REF"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_SYSTEM_CERTS=1 \
    PATH="/app/.venv/bin:$PATH" \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

WORKDIR /app
RUN apt-get update && apt-get install --no-install-recommends -y libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv==0.11.32

COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    case "$RERANK_GROUP" in rerank|rerank-gpu) ;; *) exit 2 ;; esac \
    && uv sync --frozen --no-default-groups --group "$RERANK_GROUP" --no-install-project

COPY src/ src/
COPY scripts/ scripts/
COPY migrations/ migrations/
COPY alembic.ini ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-default-groups --group "$RERANK_GROUP" \
    && python -c "from project_copilot.config import PROJECT_ROOT; assert str(PROJECT_ROOT) == '/app', PROJECT_ROOT" \
    && groupadd --gid 1000 app && useradd --uid 1000 --gid app --create-home app \
    && mkdir -p /app/data/manifests /app/models/huggingface \
    && chown -R app:app /app/data /app/models

USER app
EXPOSE 8002
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=6 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8002/health', timeout=3).read()"
CMD ["python", "-m", "uvicorn", "project_copilot.main:app", "--host", "0.0.0.0", "--port", "8002"]
