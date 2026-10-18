URL ?=
OUTPUT_DIR ?= downloads
ARGS ?=

.DEFAULT_GOAL := help
.PHONY: help install install-whisper format lint test test-integration run clean

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install: ## Install dependencies (dev included)
	uv sync

install-whisper: ## Install with optional Whisper transcription support
	uv sync --extra whisper

format: ## Format code with ruff
	uv run ruff format src tests
	uv run ruff check --fix --select I src tests

lint: format ## Run ruff and mypy
	uv run ruff check src tests
	uv run mypy src

test: ## Run unit tests
	uv run pytest

test-integration: ## Run integration tests (real YouTube + claude CLI)
	YOUTUNE_INTEGRATION=1 uv run pytest -m integration -v

run: ## Run CLI: make run URL=<youtube-url> [ARGS="--embed"]
	@test -n "$(URL)" || (echo "usage: make run URL=<youtube-url> [ARGS=...]" && exit 1)
	uv run youtune "$(URL)" --output-dir "$(OUTPUT_DIR)" $(ARGS)

clean: ## Remove caches and build artifacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache build dist
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
