#!/usr/bin/env bash
# Container entrypoint — the containerized twin of scripts/run_dev.sh.
#
# Differences from run_dev.sh (native dev):
#   --host 0.0.0.0   uvicorn's default bind is 127.0.0.1, which is unreachable
#                    from outside the container; the container's own loopback
#                    (used for inter-service calls, app/config.py ServiceSettings)
#                    still works fine since 0.0.0.0 listens on it too.
#   exec + wait -n   if any one process dies, the container exits non-zero so
#                    `docker compose`'s restart policy / healthcheck can react,
#                    instead of silently running on 6 of 7 services.
set -euo pipefail

pids=()
for spec in \
  "app.auth.service:8000" \
  "app.gateway.app:8001" \
  "app.services.crm:8002" \
  "app.services.hr:8003" \
  "app.services.devops:8004" \
  "app.services.finance:8005" \
  "app.dashboard.app:8006"; do
  module="${spec%%:*}"
  port="${spec##*:}"
  uvicorn "${module}:app" --host 0.0.0.0 --port "$port" &
  pids+=("$!")
done

trap 'kill "${pids[@]}" 2>/dev/null' EXIT INT TERM
wait -n "${pids[@]}"
exit_code=$?
echo "run_docker.sh: a service exited (code $exit_code) — stopping the rest." >&2
exit "$exit_code"
