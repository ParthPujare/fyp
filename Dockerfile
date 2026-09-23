# fyp-agent-harness — one image, one container, all seven services (Mk13 Phase 0).
#
# Runtime topology (ports 8000-8006) is unchanged from `scripts/run_dev.sh`: every
# service still talks to the others over 127.0.0.1 (`app/config.py` ServiceSettings),
# which only works because they all share this one container's network namespace.
# Splitting into one-container-per-service is future work (would need those URLs
# rewritten to service DNS names) — out of scope for "just make it run anywhere".
#
# Build:  docker compose up --build
# Image base pinned (python:3.12-slim-bookworm) and uv pinned via the official
# astral COPY pattern, both for reproducible builds — see
# https://docs.astral.sh/uv/guides/integration/docker/

FROM python:3.12-slim-bookworm

# Pinned uv binary (no separate install step, no network call at build time beyond
# this layer pull) — same version family as the maintainer's local uv (0.12.x).
COPY --from=ghcr.io/astral-sh/uv:0.12.17 /uv /uvx /bin/

ENV UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Dependency layer first (cache-friendly: only re-runs when pyproject.toml/uv.lock
# change, not on every source edit).
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project

# Application code. `.dockerignore` keeps data/, .venv/, .git/, caches and the
# host-only docs (agent_history/, research_objective/) out of the image.
COPY . .
# Belt and braces with .gitattributes: strip any CRLF from the entrypoint so a Windows
# checkout can never break `bash` inside the container.
RUN sed -i 's/\r$//' scripts/run_docker.sh
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked

ENV PATH="/app/.venv/bin:$PATH"

# Non-root runtime user. The volume-mounted data dir is created (and owned by
# appuser) here, BEFORE compose attaches a named volume at the same path — Docker
# initializes a brand-new named volume from whatever is already at that path in
# the image, ownership included.
RUN groupadd --system app && useradd --system --gid app --home-dir /app appuser \
    && mkdir -p /app/data \
    && chown -R appuser:app /app
USER appuser

# Internal ports (documentation only — compose publishes just 8006, see docker-compose.yml).
EXPOSE 8000 8001 8002 8003 8004 8005 8006

HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8006/health', timeout=2)" || exit 1

ENTRYPOINT ["bash", "scripts/run_docker.sh"]
