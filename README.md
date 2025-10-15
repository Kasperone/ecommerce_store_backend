# E-commerce Backend (FastAPI)

Backend API for e-commerce store built with FastAPI.

## 🛠️ Tech Stack

- **FastAPI** - Web framework
- **SQLAlchemy** - ORM
- **Alembic** - Database migrations
- **PostgreSQL** - Database
- **JWT** - Authentication
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

## 📚 API Documentation

- **Swagger UI:** http://localhost:8000/docs
- **ReDoc:** http://localhost:8000/redoc

## 📁 Project Structure

```
backend/
├── app/
│   ├── main.py              # FastAPI app
│   ├── core/                # Core functionality
│   │   ├── config.py        # Settings
│   │   ├── database.py      # DB connection
│   │   └── security.py      # JWT, hashing
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

## 🧪 Testing

```bash
pytest
```

## 🐳 Docker

```bash
docker-compose up
```
