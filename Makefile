.DEFAULT_GOAL := help
.PHONY: help install sync lock lint format typecheck test check run check-config clean

help:
	@echo "install       install dependencies into .venv"
	@echo "lock          refresh uv.lock"
	@echo "lint          ruff check + format check"
	@echo "format        apply ruff formatting and fixes"
	@echo "typecheck     mypy + basedpyright"
	@echo "test          pytest"
	@echo "check         lint + typecheck + test (what CI runs)"
	@echo "run           run the daemon"
	@echo "check-config  validate configuration and exit"
	@echo "clean         remove caches and build artifacts"

install:
	uv sync --all-groups

sync: install

lock:
	uv lock

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff format .
	uv run ruff check --fix .

typecheck:
	uv run mypy
	uv run basedpyright

test:
	uv run pytest

check: lint typecheck test

run:
	uv run python -m mailbridge

check-config:
	uv run python -m mailbridge --check

clean:
	rm -rf .mypy_cache .ruff_cache .pytest_cache dist build
	find . -type d -name __pycache__ -not -path "./.venv/*" -exec rm -rf {} +
