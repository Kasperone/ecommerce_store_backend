#!/usr/bin/env bash
# Run the backend in E2E mode for Playwright (runner) and Playwright MCP (authoring).
# E2E mode points at a dedicated `ecommerce_e2e` DB, no-ops Resend, and shortens
# access-token expiry so refresh-token aging tests complete in <3min.
set -euo pipefail
cd "$(dirname "$0")/.."

export DATABASE_URL="${DATABASE_URL:-postgresql://postgres:postgres@192.168.1.14:5433/ecommerce_e2e}"
export ACCESS_TOKEN_EXPIRE_MINUTES="${ACCESS_TOKEN_EXPIRE_MINUTES:-2}"
export RESEND_API_KEY=""
export ENVIRONMENT=development

exec .venv/bin/uvicorn app.main:app --port 8000 "$@"
