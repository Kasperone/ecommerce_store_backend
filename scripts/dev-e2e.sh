#!/usr/bin/env bash
# Run the backend in E2E mode for Playwright (runner) and Playwright MCP (authoring).
# E2E mode points at a dedicated `ecommerce_e2e` DB, no-ops Resend, and shortens
# access-token expiry so refresh-token aging tests complete in <3min.
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${PORT:-8000}"

# Refuse to start if something is already listening on PORT — otherwise a
# previously-launched e2e uvicorn silently keeps serving the dev frontend
# from `ecommerce_e2e`, causing 401s/empty data on dev login.
if command -v ss >/dev/null 2>&1 && ss -ltn "sport = :$PORT" | grep -q LISTEN; then
  echo "ERROR: port $PORT is already in use." >&2
  echo "       Existing listener:" >&2
  ss -ltnp "sport = :$PORT" 2>&1 | sed 's/^/         /' >&2
  echo "       Stop it before starting dev-e2e.sh, or set PORT=<other>." >&2
  exit 1
fi

export DATABASE_URL="${DATABASE_URL:-postgresql://postgres:postgres@192.168.1.14:5433/ecommerce_e2e}"
export ACCESS_TOKEN_EXPIRE_MINUTES="${ACCESS_TOKEN_EXPIRE_MINUTES:-2}"
export RESEND_API_KEY=""
export ENVIRONMENT=development

cat >&2 <<EOF
========================================================================
  E2E MODE — backend on port $PORT
  DATABASE_URL = $DATABASE_URL
  ACCESS_TOKEN_EXPIRE_MINUTES = $ACCESS_TOKEN_EXPIRE_MINUTES
  Do NOT use this for regular dev work — login uses the e2e seed users.
========================================================================
EOF

exec .venv/bin/uvicorn app.main:app --port "$PORT" "$@"
