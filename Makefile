.PHONY: install test lint run demo auth gateway crm hr devops finance

install:
	uv sync

test:
	uv run pytest

lint:
	uv run ruff check app tests

run:
	bash scripts/run_dev.sh

demo:
	uv run python scripts/demo_agent.py

auth:
	uv run uvicorn app.auth.service:app --reload --port 8000

gateway:
	uv run uvicorn app.gateway.app:app --reload --port 8001

crm:
	uv run uvicorn app.services.crm:app --reload --port 8002

hr:
	uv run uvicorn app.services.hr:app --reload --port 8003

devops:
	uv run uvicorn app.services.devops:app --reload --port 8004

finance:
	uv run uvicorn app.services.finance:app --reload --port 8005
