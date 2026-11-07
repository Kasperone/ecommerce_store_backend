# E-commerce Backend (FastAPI)

Backend API for e-commerce store built with FastAPI.

This store is my playground for learning Python and backend development as I expand from frontend toward full stack. It also lets me explore Nuxt 4 and Nuxt UI, experiment with AI coding agents, LLMs, IDEs and terminals, and use agents to test the application. It is an evolving learning project, not a production-ready store.

## Tech Stack

- **FastAPI** — Web framework
- **Python ≥ 3.13** — see `pyproject.toml`
- **SQLAlchemy 2.0** — ORM
- **Alembic** — Database migrations
- **PostgreSQL 16** — Primary database
- **Redis** — Optional shared rate-limit storage for multi-worker setups (`RATE_LIMIT_STORAGE_URL=redis://…`; `memory://` for single-process dev)
- **Meilisearch** — Product search index
- **JWT** — Authentication
- **Argon2id** — Password hashing (OWASP recommended)
- **Cloudflare R2** — Image storage (S3-compatible)
- **Resend** — Transactional email (sandbox/config only unless you wire live keys)
- **PayU + Stripe** — Payment integrations (sandbox-oriented in dev)

## Quick Start

Requires **Python ≥ 3.13**. The API process runs on the host; Docker Compose only starts infrastructure services.

### 1. Virtual environment and dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate  # Linux/macOS
# .venv\Scripts\activate   # Windows
pip install -e ".[dev]"    # runtime + test deps (or: pip install -r requirements.txt -r requirements-dev.txt)
```

### 2. Environment

```bash
cp .env.example .env
# Edit .env — see Environment Variables below; full list in .env.example
```

### 3. Infrastructure (Docker)

```bash
docker compose up -d postgres redis meilisearch
```

Postgres is exposed on `localhost:5433` by default (see `docker-compose.yml` and `.env.example`).

### 4. Migrations and server

```bash
alembic upgrade head
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

API: http://localhost:8000 — interactive docs at `/docs` (Swagger) and `/redoc`.

## Fresh install (new store bootstrap)

Prerequisites: configured `.env` (at minimum `DATABASE_URL` on an empty PostgreSQL database) and dependencies installed.

```bash
ADMIN_EMAIL='admin@example.com' ADMIN_PASSWORD='ChangeMe123' \
  python -m app.scripts.seed_all
```

What it runs, in order (each step logged `OK` / `SKIP` / `FAIL`):

1. **Migrations** — `alembic upgrade head` (skip with `--skip-migrations`)
2. **Store config** — verifies/creates the `store_config` singleton row
3. **Shipping seed** — baseline zones, methods, and rate matrix
4. **Homepage seed** — published `home` CMS page (en + pl)
5. **Initial admin** — when `ADMIN_EMAIL` and `ADMIN_PASSWORD` are both set (password via env, not argv)
6. **Search reindex** — Meilisearch indexes (fail-soft if Meili is down; `--skip-search` to skip)

Idempotent — safe to re-run.

**Demo catalog (`--demo`)** — add `--demo` to `seed_all` or run `python -m app.scripts.seed_demo`. Objects use a `demo-` slug prefix (coupon `DEMO10`, SKUs `DEMO-*`). Remove them before go-live via the admin catalog pages, or in SQL start from `SELECT id FROM products WHERE slug LIKE 'demo-%'` (same `LIKE 'demo-%'` pattern for `categories` and `collections`, plus the `DEMO10` coupon).

Create or rotate an admin alone:

```bash
ADMIN_PASSWORD='ChangeMe123' python -m app.scripts.create_admin --email admin@example.com --name "Store Admin"
ADMIN_PASSWORD='NewPass456' python -m app.scripts.create_admin --email admin@example.com --update-password
```

Exit codes: `0` ok/no-op, `1` unexpected error, `2` usage/validation error.

## API documentation

All routes are under `/api/v1/`. Use the generated OpenAPI UI instead of a hand-maintained list:

- **Swagger UI:** http://localhost:8000/docs
- **ReDoc:** http://localhost:8000/redoc

Examples: `POST /api/v1/auth/register`, `POST /api/v1/auth/login`, `GET /api/v1/products`, `POST /api/v1/orders`.

Manual exploration: `bruno/` Bruno collection (see `bruno/README.md`).

## Frontend integration

Frontend repository: https://github.com/Kasperone/ecommerce_store_frontend

```bash
# Backend (port 8000)
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Frontend (port 3000), sibling repo
cd ../ecommerce_store_frontend && npm run dev
```

CORS accepts the frontend origin (e.g. `http://localhost:3000`) via settings in `.env` / `app/core/config.py`.

## Project structure

```
app/
  api/v1/       # Route modules (admin_* for admin)
  core/         # Config, database, security, rate limits, money, email helpers
  crud/         # DB operations
  models/       # SQLAlchemy models
  schemas/      # Pydantic schemas
  services/     # Business logic (search, invoices, pricing, …)
  scripts/      # CLIs: python -m app.scripts.<name>
  main.py       # FastAPI entry
alembic/        # Migrations
tests/          # pytest suite
bruno/          # API request collection
```

## Environment variables

Copy `.env.example` to `.env`. **Names and placeholders only** — see `.env.example` for the complete list.

```env
DATABASE_URL=postgresql://postgres:postgres@localhost:5433/ecommerce_db

JWT_SECRET_KEY=your-jwt-secret-key-here-change-in-production-must-be-32-bytes
JWT_ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=30

RESEND_API_KEY=
EMAIL_FROM=noreply@yourdomain.com

R2_ACCOUNT_ID=
R2_ACCESS_KEY_ID=
R2_SECRET_ACCESS_KEY=
R2_BUCKET_NAME=

STRIPE_SECRET_KEY=sk_test_your_stripe_key
STRIPE_PUBLISHABLE_KEY=pk_test_your_stripe_key
STRIPE_WEBHOOK_SECRET=whsec_your_webhook_secret

RATE_LIMIT_STORAGE_URL=memory://
CORS_ORIGINS=["http://localhost:3000"]
```

## Overview

REST API for catalog, cart/checkout flows, orders, payments (sandbox-configured), admin capabilities, search, and CMS-backed content — see OpenAPI at `/docs` for the full surface.

## Testing

Install dev dependencies first (`pip install -e ".[dev]"` or `requirements-dev.txt`). Tests expect `.env.test` and a dedicated `ecommerce_test` database — see `.claude/rules/testing.md`.

```bash
pytest
```

## Docker

`docker-compose.yml` starts **postgres**, **redis**, and **meilisearch** only. Run **uvicorn** on the host (or your own container image) for the API.

```bash
docker compose up -d postgres redis meilisearch
```

## Security notes

- **Rate limiting** — Enabled via slowapi (`app/main.py`, `app/core/rate_limit.py`). Storage URL from `RATE_LIMIT_STORAGE_URL`. Disabled only when `ENVIRONMENT=development` **and** `E2E_DISABLE_RATE_LIMIT=true`.
- **TLS** — The app does not terminate HTTPS. In production it may send an **HSTS** header when `ENVIRONMENT=production` (`app/core/security_headers.py`); TLS is expected from a reverse proxy in front of the API.
- JWT expiry, Argon2id passwords, CORS, ORM parameterization — see `app/core/security.py` and `CLAUDE.md`.

## Technologies & resources

- [FastAPI](https://fastapi.tiangolo.com/)
- [SQLAlchemy 2.0](https://docs.sqlalchemy.org/)
- [Alembic](https://alembic.sqlalchemy.org/)
- [PyJWT](https://pyjwt.readthedocs.io/)
