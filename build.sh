#!/bin/sh

set -euo pipefail
uv sync --frozen --dev
uv run pyright
uv run ruff check
uv run ruff format --check
uv run pytest
uv sync --frozen --no-editable