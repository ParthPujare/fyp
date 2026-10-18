#!/usr/bin/env bash
# Launch all seven local processes on ports 8000-8006 (runtime topology, see ref_docs/proposed_architecture.md §2).
set -euo pipefail

# Mk19 Phase 4: bare-metal dev keeps the repo-public dev secrets working via the
# explicit hatch (default 1 here, in docker-compose.yml and .env.example).
# ENV=prod still refuses the built-ins and the hatch itself, regardless of this.
export ALLOW_DEV_SECRETS="${ALLOW_DEV_SECRETS:-1}"

# Shared database (Mk13): migrate first if DATABASE_URL is configured (no-op otherwise).
uv run python -m app.db migrate --if-configured

uv run uvicorn app.auth.service:app --port 8000 &
uv run uvicorn app.gateway.app:app --port 8001 &
uv run uvicorn app.services.crm:app --port 8002 &
uv run uvicorn app.services.hr:app --port 8003 &
uv run uvicorn app.services.devops:app --port 8004 &
uv run uvicorn app.services.finance:app --port 8005 &
uv run uvicorn app.dashboard.app:app --port 8006 &

trap 'kill 0' EXIT INT TERM
wait
