# syntax=docker/dockerfile:1
FROM python:3.11-slim

# Bring in the uv binary from the official image (no pip bootstrap needed).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy

RUN useradd --create-home --uid 10001 appuser
RUN chown appuser:appuser /app

USER appuser

# Install dependencies first for layer caching. Use --frozen so uv installs exactly
# the CPU-only locked dependencies from uv.lock (torch CPU wheel, no NVIDIA/CUDA/triton)
# and never re-resolves to the full CUDA torch stack. --no-install-project installs only
# third-party deps here so this layer stays cached and does not need the source tree yet.
COPY --chown=appuser:appuser pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Now copy the source and install the project itself against it.
COPY --chown=appuser:appuser . .
RUN uv sync --frozen --no-dev

EXPOSE 8000

# Dependencies are already installed at build time (uv sync --frozen above).
# --no-sync tells uv run NOT to re-run uv sync at container startup, which
# previously caused a 40+ second cold-start and triggered healthcheck failures.
HEALTHCHECK --interval=15s --timeout=5s --retries=3 --start-period=30s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5)"

# 4 workers: one per CPU core available on the dev machine.
# In production this would be set by the orchestration layer.
CMD ["uv", "run", "--no-sync", "uvicorn", "src.api.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
