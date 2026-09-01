#!/usr/bin/env bash
# Start the API and the UI on :3000. Ctrl-C stops both.
#
# Defaults suit local development. Override for a server deployment:
#   HOST=0.0.0.0 PORT=5249 ./dev.sh
set -euo pipefail
cd "$(dirname "$0")"

HOST=${HOST:-127.0.0.1}
PORT=${PORT:-5000}

PY=${PY:-$([ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)}

echo "API on http://$HOST:$PORT  (docs at /docs)"
"$PY" -m uvicorn backend.main:app --reload --host "$HOST" --port "$PORT" &
API=$!
trap 'kill $API 2>/dev/null || true' EXIT

npm --prefix frontend run dev
