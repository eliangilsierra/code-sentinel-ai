.PHONY: install test lint fmt eval-smoke

install:
	uv sync

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff format .
	uv run ruff check --fix .

eval-smoke:
	uv run python -m evals.runner run --suite smoke
