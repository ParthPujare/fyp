.PHONY: install test lint run demo demo-memory demo-sanitize demo-skills demo-evals demo-attacks auth gateway dashboard crm hr devops finance

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
