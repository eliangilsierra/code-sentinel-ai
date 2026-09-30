# review-squad

Code review assistant for Claude Code that prepares review context deterministically, requires
cited evidence for every finding and publishes only findings that pass a mechanical gate.

## Requirements

- git
- Python 3.10 or later and [uv](https://docs.astral.sh/uv/)
- Claude Code 2.1.269 or later

## Development

| Task | With make | Without make |
|---|---|---|
| Install dependencies | `make install` | `uv sync` |
| Run tests | `make test` | `uv run pytest` |
| Lint | `make lint` | `uv run ruff check . && uv run ruff format --check .` |
| Format | `make fmt` | `uv run ruff format . && uv run ruff check --fix .` |
| Run the eval smoke suite | `make eval-smoke` | `uv run python -m evals.runner run --suite smoke` |
