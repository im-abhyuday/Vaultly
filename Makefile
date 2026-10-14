.PHONY: help install lint format type-check test test-cov run-api run-worker migrate seed clean docker-up docker-down

help:
	@echo "TenantRAG Development Commands:"
	@echo "  make install      - Install backend dependencies"
	@echo "  make lint         - Run ruff linter"
	@echo "  make format       - Format code with ruff"
	@echo "  make type-check   - Run mypy strict type checks"
	@echo "  make test         - Run pytest suite"
	@echo "  make test-cov     - Run pytest with coverage report"
	@echo "  make docker-up    - Start all services with docker-compose"
	@echo "  make docker-down  - Stop all services"
	@echo "  make migrate      - Run Alembic migrations to head"
	@echo "  make run-api      - Run FastAPI server locally"
	@echo "  make run-worker   - Run Kafka workers locally"

install:
	pip install -r backend/requirements-dev.txt

lint:
	ruff check backend

format:
	ruff format backend

type-check:
	mypy backend/app backend/tests

test:
	pytest backend/tests -v

test-cov:
	pytest backend/tests -v --cov=backend/app --cov-report=term-missing

docker-up:
	docker compose up -d

docker-down:
	docker compose down -v

migrate:
	cd backend && alembic upgrade head

run-api:
	cd backend && uvicorn app.main:app --reload --port 8000

run-worker:
	cd backend && python -m app.workers.main

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type d -name ".pytest_cache" -exec rm -rf {} +
	find . -type d -name ".mypy_cache" -exec rm -rf {} +
	find . -type d -name ".ruff_cache" -exec rm -rf {} +
