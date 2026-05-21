# Schema Drift Detective — developer entry points.
# All targets are .PHONY: there is no file-target makefile here on purpose.
.PHONY: help install up down demo bench fmt lint typecheck test test-fast cov clean reset
.DEFAULT_GOAL := help

# Use bash for richer shell features; -e fails on the first error.
SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c

VENV    ?= .venv
PY      ?= $(VENV)/bin/python
PIP     ?= $(VENV)/bin/pip

help: ## Show this help message.
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

$(VENV)/bin/python:
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip wheel

install: $(VENV)/bin/python ## Install dev + llm + dbt extras into local venv.
	$(PIP) install -e ".[dev,llm,slack,lineage,dbt]"

up: ## Start local Postgres (and other services) via docker compose.
	docker compose up -d
	@echo "Waiting for Postgres to be ready..."
	@until docker compose exec -T postgres pg_isready -U drift -d drift >/dev/null 2>&1; do sleep 1; done
	@echo "Postgres is up. psql: postgresql://drift:drift@localhost:5432/drift"

down: ## Stop services (keeps volumes).
	docker compose down

reset: ## Stop services and WIPE volumes (destructive).
	docker compose down -v

demo: install up ## End-to-end demo: inject a drift, open a PR (dry-run by default).
	$(PY) -m schema_drift.cli demo --dry-run

bench: install ## Run the full benchmark on the held-out split.
	$(PY) -m bench.runner --all --split holdout

fmt: ## Auto-format code with ruff.
	$(VENV)/bin/ruff format src tests bench
	$(VENV)/bin/ruff check --fix src tests bench

lint: ## Lint with ruff (no autofix).
	$(VENV)/bin/ruff check src tests bench
	$(VENV)/bin/ruff format --check src tests bench

typecheck: ## Static type check with pyright.
	$(VENV)/bin/pyright src tests bench

test-fast: ## Run fast unit tests only (skip slow + integration).
	$(VENV)/bin/pytest -m "not slow and not integration" -q

test: ## Run all tests (includes integration when docker is up).
	$(VENV)/bin/pytest

cov: ## Run tests and open coverage report in browser.
	$(VENV)/bin/pytest --cov-report=html
	@command -v open >/dev/null && open htmlcov/index.html || true

clean: ## Remove caches and build artifacts.
	rm -rf .pytest_cache .ruff_cache .mypy_cache .pyright build dist *.egg-info htmlcov coverage.xml
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
