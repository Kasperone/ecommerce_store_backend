#!/usr/bin/env bash
# Run the backend in regular dev mode. Loads .env (ecommerce_db) and
# explicitly UNsets any e2e-mode env vars that may have leaked from a
# previous `dev-e2e.sh` source/run in the same shell.
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${PORT:-8000}"

if command -v ss >/dev/null 2>&1 && ss -ltn "sport = :$PORT" | grep -q LISTEN; then
  echo "ERROR: port $PORT is already in use." >&2
  echo "       Existing listener:" >&2
  ss -ltnp "sport = :$PORT" 2>&1 | sed 's/^/         /' >&2
  echo "       Stop it before starting dev.sh, or set PORT=<other>." >&2
  exit 1
fi

# Strip any e2e leftovers so pydantic Settings falls back to .env values.
unset DATABASE_URL ACCESS_TOKEN_EXPIRE_MINUTES RESEND_API_KEY

cat >&2 <<EOF
========================================================================
  DEV MODE — backend on port $PORT
  DATABASE_URL is loaded from .env (expected: ecommerce_db)
========================================================================
EOF

exec .venv/bin/uvicorn app.main:app --reload --port "$PORT" "$@"
