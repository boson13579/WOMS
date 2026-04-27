# =============================================================================
# Smart Order Management — one-stop developer entrypoint.
#
#   make setup     install all backend + frontend deps and pre-commit hooks
#   make up        start the full docker-compose stack
#   make down      stop and remove containers (data volumes preserved)
#   make migrate   run alembic upgrade head against the running db
#   make revision  create a new alembic revision (m="describe change")
#   make test      run backend pytest + frontend vitest
#   make lint      run ruff + mypy + eslint + prettier checks
#   make seed      load fixture data (root user, sample orders) — Phase 2+
#   make logs      tail container logs
#   make clean     remove caches (.venv stays; node_modules stays)
#
# All recipes use POSIX shell. On Windows, run via WSL2 or Git Bash.
# =============================================================================

SHELL := /usr/bin/env bash
.ONESHELL:
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

COMPOSE       := docker compose
BACKEND_DIR   := backend
FRONTEND_DIR  := frontend

# Bail out early if .env is missing — it's required by docker-compose.yml.
ENV_FILE := .env

.PHONY: help
help: ## Show this help.
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
.PHONY: env
env: ## Copy .env.example to .env if it doesn't exist yet.
	@test -f $(ENV_FILE) && echo ".env already exists — skipping." || cp .env.example $(ENV_FILE)

.PHONY: setup
setup: env ## Install all dependencies (uv + pnpm + pre-commit).
	@echo "==> Backend (uv sync)"
	cd $(BACKEND_DIR) && uv sync --all-extras
	@echo "==> Frontend (pnpm install)"
	cd $(FRONTEND_DIR) && pnpm install --frozen-lockfile || pnpm install
	@echo "==> Pre-commit hooks"
	pre-commit install || echo "(pre-commit not installed yet — run: uv tool install pre-commit)"
	@echo "==> Done."

# ---------------------------------------------------------------------------
# Docker compose lifecycle
# ---------------------------------------------------------------------------
.PHONY: up
up: ## Start all services (db, redis, backend, worker, frontend).
	$(COMPOSE) up -d
	@echo ""
	@echo "  Backend:  http://localhost:8000/api/v1/health"
	@echo "  Frontend: http://localhost:5173"
	@echo "  Postgres: localhost:5432"
	@echo ""

.PHONY: down
down: ## Stop services (volumes preserved).
	$(COMPOSE) down

.PHONY: nuke
nuke: ## Stop services AND drop all data volumes (DANGER — wipes db).
	$(COMPOSE) down -v

.PHONY: logs
logs: ## Tail logs from all services.
	$(COMPOSE) logs -f --tail=100

.PHONY: ps
ps: ## Show service health.
	$(COMPOSE) ps

# ---------------------------------------------------------------------------
# Database migrations
# ---------------------------------------------------------------------------
.PHONY: migrate
migrate: ## Apply all pending alembic migrations.
	$(COMPOSE) exec backend alembic upgrade head

.PHONY: revision
revision: ## Create a new alembic revision (use: make revision m="add orders").
	@test -n "$(m)" || (echo "Usage: make revision m=\"describe change\""; exit 1)
	$(COMPOSE) exec backend alembic revision --autogenerate -m "$(m)"

.PHONY: downgrade
downgrade: ## Roll back one alembic revision.
	$(COMPOSE) exec backend alembic downgrade -1

# ---------------------------------------------------------------------------
# Quality gates
# ---------------------------------------------------------------------------
.PHONY: test
test: test-backend test-frontend ## Run all tests.

.PHONY: test-backend
test-backend: ## Backend pytest (uses Testcontainers — needs Docker).
	cd $(BACKEND_DIR) && uv run pytest

.PHONY: test-frontend
test-frontend: ## Frontend Vitest.
	cd $(FRONTEND_DIR) && pnpm test

.PHONY: lint
lint: lint-backend lint-frontend ## Run all linters and type checkers.

.PHONY: lint-backend
lint-backend:
	cd $(BACKEND_DIR) && uv run ruff check . && uv run ruff format --check . && uv run mypy app

.PHONY: lint-frontend
lint-frontend:
	cd $(FRONTEND_DIR) && pnpm lint && pnpm typecheck

.PHONY: format
format: ## Auto-format everything (ruff + prettier).
	cd $(BACKEND_DIR) && uv run ruff check --fix . && uv run ruff format .
	cd $(FRONTEND_DIR) && pnpm format

# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
.PHONY: seed
seed: ## Load development fixture data (Phase 2+).
	@echo "Phase 1 placeholder — add fixture loader when entities are introduced."

# ---------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------
.PHONY: clean
clean: ## Remove caches (preserves .venv and node_modules).
	find . -type d -name '__pycache__' -exec rm -rf {} +
	find . -type d -name '.pytest_cache' -exec rm -rf {} +
	find . -type d -name '.mypy_cache' -exec rm -rf {} +
	find . -type d -name '.ruff_cache' -exec rm -rf {} +
	rm -rf $(BACKEND_DIR)/coverage.xml $(BACKEND_DIR)/htmlcov $(FRONTEND_DIR)/coverage
