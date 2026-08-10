#!/usr/bin/env bash
# Launch all six local processes on ports 8000-8005 (runtime topology, see ref_docs/proposed_architecture.md §2).
set -euo pipefail

uv run uvicorn app.auth.service:app --port 8000 &
uv run uvicorn app.gateway.app:app --port 8001 &
uv run uvicorn app.services.crm:app --port 8002 &
uv run uvicorn app.services.hr:app --port 8003 &
uv run uvicorn app.services.devops:app --port 8004 &
uv run uvicorn app.services.finance:app --port 8005 &

trap 'kill 0' EXIT INT TERM
wait
