#!/usr/bin/env bash
# Start the API on :8000 and the UI on :3000. Ctrl-C stops both.
set -euo pipefail
cd "$(dirname "$0")"

PY=${PY:-$([ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)}

"$PY" -m uvicorn backend.main:app --reload --port 8000 &
API=$!
trap 'kill $API 2>/dev/null || true' EXIT

npm --prefix frontend run dev
