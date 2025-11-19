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
