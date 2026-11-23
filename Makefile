# Developer shortcuts. Run `make help` for the list.
.DEFAULT_GOAL := help
COMPOSE := docker compose
BACKEND := cd backend &&
FRONTEND := cd frontend &&

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

.env:
	cp .env.example .env
	@echo "Created .env from .env.example (development placeholders)."

.PHONY: setup
setup: .env ## Install backend and frontend dependencies locally
	$(BACKEND) uv sync --frozen
	$(FRONTEND) npm ci

.PHONY: up
up: .env ## Start the full stack in Docker
	$(COMPOSE) up -d --build

.PHONY: infra
infra: .env ## Start only PostgreSQL, Redis and MinIO
	$(COMPOSE) up -d --wait postgres redis minio

.PHONY: down
down: ## Stop the stack (keeps data volumes)
	$(COMPOSE) down

.PHONY: logs
logs: ## Follow logs from all services
	$(COMPOSE) logs -f

.PHONY: migrate
migrate: ## Apply database migrations (native backend)
	$(BACKEND) uv run alembic upgrade head

.PHONY: bootstrap
bootstrap: migrate ## Apply migrations and create the storage bucket
	$(BACKEND) uv run documind-manage bootstrap-storage

.PHONY: check
check: ## Check dependency health from the CLI
	$(BACKEND) uv run documind-manage check

.PHONY: backend-dev
backend-dev: ## Run the API natively with reload
	$(BACKEND) uv run uvicorn documind.asgi:app --reload --reload-dir src

.PHONY: frontend-dev
frontend-dev: ## Run the Vite dev server natively
	$(FRONTEND) npm run dev

.PHONY: lint
lint: ## Lint and type-check everything
	$(BACKEND) uv run ruff check . && uv run ruff format --check . && uv run mypy
	$(FRONTEND) npm run lint && npm run typecheck

.PHONY: test
test: ## Run backend unit tests and frontend tests
	$(BACKEND) uv run pytest
	$(FRONTEND) npm test

.PHONY: test-integration
test-integration: ## Run backend integration tests (needs `make infra bootstrap`)
	$(BACKEND) uv run pytest -m integration
