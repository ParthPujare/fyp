.PHONY: db-check db-migrate db-status db-prune db-sync-config test-db-up test-db-down test-pg test-pg-full install test lint run demo demo-memory demo-sanitize demo-skills demo-evals demo-attacks demo-chat-web demo-external auth gateway dashboard crm hr devops finance

install:
	uv sync

test:
	uv run pytest

lint:
	uv run ruff check app tests scripts

run:
	bash scripts/run_dev.sh

demo:
	uv run python scripts/demo_agent.py

demo-memory:
	uv run python scripts/demo_memory.py

demo-sanitize:
	uv run python scripts/demo_sanitize_dashboard.py

demo-skills:
	uv run python scripts/demo_skills.py

demo-evals:
	uv run python scripts/demo_trajectory_evals.py

demo-attacks:
	uv run python scripts/demo_attacks.py

demo-chat-web:
	uv run python scripts/demo_chat_web.py

demo-external:
	uv run python scripts/demo_external.py

auth:
	uv run uvicorn app.auth.service:app --reload --port 8000

gateway:
	uv run uvicorn app.gateway.app:app --reload --port 8001

dashboard:
	uv run uvicorn app.dashboard.app:app --reload --port 8006

crm:
	uv run uvicorn app.services.crm:app --reload --port 8002

hr:
	uv run uvicorn app.services.hr:app --reload --port 8003

devops:
	uv run uvicorn app.services.devops:app --reload --port 8004

finance:
	uv run uvicorn app.services.finance:app --reload --port 8005

# ---- Shared database (Mk13) ----
db-check:            ## connect to DATABASE_URL and report (host, latency, migrations)
	uv run python -m app.db check

db-migrate:          ## apply pending migrations (containers do this at start-up)
	uv run python -m app.db migrate

db-status:
	uv run python -m app.db status

db-prune:           ## free space: keep only each chat's latest checkpoint (add --traces-older-than N via ARGS)
	uv run python -m app.db prune --chats $(ARGS)

db-sync-config:      ## copy policy.csv / skills/ changes into the shared DB (ARGS="--dry-run", "--prune", "--overwrite-skills")
	uv run python -m app.db sync-config $(ARGS)

test-db-up:          ## throwaway local Postgres for the Postgres-backed tests (needs Docker)
	docker compose --profile test-db up -d --wait testdb

test-db-down:
	docker compose --profile test-db rm -sf testdb

test-pg:             ## run the whole suite INCLUDING the Postgres tests (starts/stops the throwaway DB)
	$(MAKE) test-db-up
	TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:55432/postgres uv run pytest; status=$$?; $(MAKE) test-db-down; exit $$status

test-pg-full:        ## whole suite with the MEMBER STORE on Postgres (regression check that it is a drop-in)
	$(MAKE) test-db-up
	TEST_BACKEND=postgres TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:55432/postgres uv run pytest; status=$$?; $(MAKE) test-db-down; exit $$status
