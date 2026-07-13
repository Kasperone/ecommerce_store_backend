# E-commerce Backend (FastAPI)

Backend API for e-commerce store built with FastAPI.

## 🛠️ Tech Stack

- **FastAPI** - Web framework
- **SQLAlchemy** - ORM
- **Alembic** - Database migrations
- **PostgreSQL** - Database
- **JWT** - Authentication
- **Argon2id** - Password hashing (OWASP recommended)
- **Cloudflare R2** - Image storage
- **Resend** - Email service
- **PayU + Stripe** - Payments

## 🚀 Quick Start

### 1. Create virtual environment

```bash
python3 -m venv venv
source venv/bin/activate  # Linux/Mac
# venv\Scripts\activate   # Windows
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Setup environment variables

```bash
cp .env.example .env
# Edit .env with your configuration
```

### 4. Run database (Docker)

```bash
docker-compose up -d postgres
```

### 5. Run migrations

```bash
alembic upgrade head
```

### 6. Start development server

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

API will be available at: http://localhost:8000

## 🏗️ Fresh install (new store bootstrap)

Bring up a brand-new client store with one command (replaces steps 5+ above
for a first deploy). Prerequisites: a configured `.env` (copy `.env.example`;
at minimum `DATABASE_URL` pointing at an empty PostgreSQL database) and the
dependencies installed.

```bash
ADMIN_EMAIL='admin@example.com' ADMIN_PASSWORD='ChangeMe123' \
    python -m app.scripts.seed_all
```

What it runs, in order (each step logged `OK` / `SKIP` / `FAIL`):

1. **Migrations** — `alembic upgrade head` (skip with `--skip-migrations`)
2. **Store config** — verifies/creates the `store_config` singleton row
   (edit it later via the admin Settings page)
3. **Shipping seed** — baseline zones (PL / EU / RoW), `standard` + `express`
   methods with EN+PL copy, and a rate matrix in PLN/EUR/USD
4. **Homepage seed** — a published `home` CMS page (en + pl) the storefront
   renders; customize it in the admin page builder
5. **Initial admin** — a pre-verified `ADMIN` user, only when `ADMIN_EMAIL`
   and `ADMIN_PASSWORD` are both set (otherwise the step is skipped with a
   hint). The password must satisfy the registration policy (min 8 chars,
   a digit and an uppercase letter) and is never accepted via argv
6. **Search reindex** — rebuilds the Meilisearch indexes. Fail-soft: if
   Meilisearch is unconfigured or unreachable this logs a warning and the
   bootstrap still exits 0 (skip explicitly with `--skip-search`; rerun
   later with `python -m app.scripts.reindex_search`)

**Idempotent by design** — every step is get-or-create, so re-running the
command on an existing store is safe: admin-edited shipping rates, homepage
content, store config, and the existing admin's password are all left
untouched (steps report `OK`/`SKIP`, nothing is duplicated or overwritten).

**Demo catalog (`--demo`)** — add `--demo` to also seed a small browsable
demo catalog for client demos (inserted before the search reindex, so demo
products get indexed too): 3 categories, 6 products (multi-currency prices,
full EN+PL copy, one reduced-VAT item, one with a Size × Color variant
matrix), a `demo-bestsellers` collection and a `DEMO10` coupon (10% off).
Everything uses a `demo-` slug prefix (coupon `DEMO10`, SKUs `DEMO-*`), so
it is easy to find and remove before go-live — either deactivate/delete via
the admin catalog pages, or in SQL start from
`SELECT id FROM products WHERE slug LIKE 'demo-%'` (same `LIKE 'demo-%'`
pattern for `categories` and `collections`, plus the `DEMO10` coupon). The
demo step is idempotent like the rest: existing `demo-` objects are skipped,
never duplicated or updated. Standalone rerun: `python -m app.scripts.seed_demo`.

To create or rotate an admin on its own:

```bash
# create (no-op if the email already exists)
ADMIN_PASSWORD='ChangeMe123' python -m app.scripts.create_admin --email admin@example.com --name "Store Admin"

# explicitly rotate an existing admin's password (revokes their sessions)
ADMIN_PASSWORD='NewPass456' python -m app.scripts.create_admin --email admin@example.com --update-password
```

The password comes from the `ADMIN_PASSWORD` env var or piped stdin — never
from a CLI argument. Exit codes: `0` ok/no-op, `1` unexpected error, `2`
usage/validation error.

## 📚 API Documentation

- **Swagger UI:** http://localhost:8000/docs
- **ReDoc:** http://localhost:8000/redoc

## 🔗 Frontend Integration

Frontend repository: https://github.com/Kasperone/ecommerce_store_frontend

### Running Both Services Locally

In separate terminals:

```bash
# Backend (port 8000)
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Frontend (port 3000)
cd ../frontend
npm run dev
```

Note: Frontend expects backend API at http://localhost:8000 (configured in `.env.local`)

### CORS Configuration

Backend CORS is configured to accept requests from:
- http://localhost:3000 (development frontend)
- Production domain (set via environment variables)

## 📁 Project Structure

```
backend/
├── app/
│   ├── main.py              # FastAPI app
│   ├── core/                # Core functionality
│   │   ├── config.py        # Settings
│   │   ├── database.py      # DB connection
│   │   └── security.py      # JWT, Argon2id hashing
│   ├── models/              # SQLAlchemy models
│   │   ├── user.py
│   │   ├── product.py
│   │   └── order.py
│   ├── schemas/             # Pydantic schemas
│   │   ├── user.py
│   │   └── product.py
│   ├── api/                 # API routes
│   │   └── v1/
│   │       ├── auth.py
│   │       ├── products.py
│   │       └── orders.py
│   └── services/            # Business logic
│       ├── storage.py       # R2 integration
│       ├── email.py         # Resend integration
│       └── payment.py       # PayU/Stripe
├── alembic/                 # Database migrations
├── tests/
├── requirements.txt
└── README.md
```

## 🔐 Environment Variables

Create `.env` file in the backend root. Key variables:

```env
# Database
DATABASE_URL=postgresql://user:password@localhost:5432/ecommerce

# JWT
SECRET_KEY=your-secret-key-here
ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=30

# Email (Resend)
RESEND_API_KEY=your-resend-api-key
RESEND_FROM_EMAIL=noreply@example.com

# Storage (Cloudflare R2)
R2_ACCOUNT_ID=your-account-id
R2_ACCESS_KEY_ID=your-access-key
R2_SECRET_ACCESS_KEY=your-secret-key
R2_BUCKET_NAME=your-bucket-name

# Payment
PAYU_CLIENT_ID=your-payu-client-id
PAYU_CLIENT_SECRET=your-payu-secret
STRIPE_API_KEY=your-stripe-key

# CORS
CORS_ORIGINS=["http://localhost:3000"]
```

## 🧪 Testing

```bash
pytest
```

## 🐳 Docker

```bash
docker-compose up
```

## 📝 API Endpoints

### Authentication
- POST /api/v1/auth/register - User registration
- POST /api/v1/auth/login - User login
- POST /api/v1/auth/verify-email - Email verification
- POST /api/v1/auth/refresh - Token refresh
- POST /api/v1/auth/logout - User logout

### Products
- GET /api/v1/products - List products
- GET /api/v1/products/{id} - Get product details
- POST /api/v1/products - Create product (admin)
- PUT /api/v1/products/{id} - Update product (admin)
- DELETE /api/v1/products/{id} - Delete product (admin)

### Orders
- GET /api/v1/orders - List user orders
- POST /api/v1/orders - Create order
- GET /api/v1/orders/{id} - Get order details

## ✨ Features Implemented

### Authentication & Security
- User registration with email verification
- JWT-based authentication with refresh tokens
- Argon2id password hashing (OWASP recommended)
- Secure cookie handling
- Token revocation on logout

### Email Verification
- Verification tokens with expiration
- Resend email service integration
- Email templates
- Verification link validation

### Payment Integration
- PayU payment gateway
- Stripe integration
- Payment status tracking
- Order history

### Media Management
- Cloudflare R2 integration for image storage
- Image upload and processing
- CDN delivery

## 🛡️ Security

- HTTPS/SSL in production
- JWT token expiration
- Password hashing with Argon2id
- CORS protection
- Rate limiting ready
- SQL injection prevention (SQLAlchemy ORM)

## 📚 Technologies & Resources

- FastAPI Documentation: https://fastapi.tiangolo.com/
- SQLAlchemy Documentation: https://docs.sqlalchemy.org/
- JWT in Python: https://pyjwt.readthedocs.io/
- Alembic Migrations: https://alembic.sqlalchemy.org/
