# Pydantic Schemas Documentation

Schemas służą do walidacji danych wejściowych i wyjściowych w API. Są oddzielne od modeli SQLAlchemy.

## 📁 Struktura

```
app/schemas/
├── __init__.py          # Główny eksport
├── base.py             # Bazowe klasy
├── user.py             # User schemas
├── category.py         # Category schemas
├── product.py          # Product schemas
├── order.py            # Order schemas
└── auth.py             # Authentication schemas
```

## 🔑 Kluczowe koncepcje

### **Base schemas**
- `BaseSchema` - bazowa konfiguracja dla wszystkich schemas
- `TimestampSchema` - dodaje `id`, `created_at`, `updated_at`

### **Naming convention**
- `*Base` - wspólne pola
- `*Create` - do tworzenia (POST)
- `*Update` - do aktualizacji (PATCH/PUT)
- `*Response` - do zwracania w API
- `*InDB` - pełna reprezentacja z bazy (z hasłem, etc.)

## 📦 Przykłady użycia

### **User schemas**

```python
from app.schemas import UserCreate, UserResponse

# Request body dla rejestracji
user_data = {
    "email": "user@example.com",
    "password": "SecurePass123",
    "first_name": "John",
    "last_name": "Doe"
}
user = UserCreate(**user_data)

# Response (bez hasła)
response = UserResponse.from_orm(db_user)
```

**Walidacje:**
- Email: format email (EmailStr)
- Password: min 8 znaków, 1 cyfra, 1 wielka litera

---

### **Product schemas**

```python
from app.schemas import ProductCreate, ProductResponse

# Request body dla dodania produktu
product_data = {
    "name": "Nike Air Max",
    "slug": "nike-air-max",
    "sku": "NIKE-AM-001",
    "price_usd": 120.00,
    "price_pln": 480.00,
    "price_eur": 110.00,
    "stock": 50,
    "images": ["https://example.com/img1.jpg"],
    "category_id": 1
}
product = ProductCreate(**product_data)
```

**Walidacje:**
- Ceny: > 0
- Stock: >= 0
- Images: max 10 URL-i
- Slug: unikalny, URL-friendly

---

### **Order schemas**

```python
from app.schemas import OrderCreate

# Request body dla zamówienia
order_data = {
    "items": [
        {"product_id": 1, "quantity": 2},
        {"product_id": 5, "quantity": 1}
    ],
    "currency": "PLN",
    "shipping_address": "ul. Główna 10",
    "shipping_city": "Warszawa",
    "shipping_postal_code": "00-001",
    "shipping_country": "Poland"
}
order = OrderCreate(**order_data)
```

**Automatycznie wyliczane:**
- `subtotal` - suma cen produktów
- `total` - subtotal + tax + shipping
- `order_number` - unikalny numer zamówienia

---

### **Category schemas**

```python
from app.schemas import CategoryCreate

category_data = {
    "name": "Electronics",
    "slug": "electronics",
    "description": "Electronic devices and gadgets",
    "is_active": True
}
category = CategoryCreate(**category_data)
```

---

## 🔐 Authentication schemas

### **Token**
```python
from app.schemas import Token

# Response po zalogowaniu
{
    "access_token": "eyJhbGciOiJIUzI1NiIs...",
    "token_type": "bearer"
}
```

### **UserLogin**
```python
from app.schemas import UserLogin

login_data = {
    "email": "user@example.com",
    "password": "SecurePass123"
}
```

---

## 📊 Response schemas z relacjami

### **ProductWithCategory**
```python
from app.schemas import ProductWithCategory

# Zwraca produkt + dane kategorii
{
    "id": 1,
    "name": "Nike Air Max",
    "category": {
        "id": 1,
        "name": "Shoes",
        "slug": "shoes"
    }
}
```

### **OrderWithUser**
```python
from app.schemas import OrderWithUser

# Zwraca zamówienie + dane użytkownika
{
    "id": 1,
    "order_number": "ORD-2024-001",
    "user": {
        "id": 1,
        "email": "user@example.com",
        "first_name": "John"
    },
    "items": [...]
}
```

---

## 📄 Pagination schemas

Wszystkie listy mają standardowy format:

```python
from app.schemas import ProductListResponse

{
    "items": [...],      # Lista produktów
    "total": 100,        # Łączna liczba
    "page": 1,           # Aktualna strona
    "page_size": 20,     # Rozmiar strony
    "pages": 5           # Łączna liczba stron
}
```

Podobnie: `OrderListResponse`, `CategoryWithProductCount`

---

## 🎯 Best practices

1. **Używaj właściwych schemas w endpoints:**
   - `*Create` dla POST
   - `*Update` dla PATCH/PUT
   - `*Response` dla zwracanych danych

2. **Nie zwracaj haseł w response:**
   - `UserResponse` nie ma `hashed_password`
   - `UserInDB` używaj tylko wewnętrznie

3. **Waliduj dane wejściowe:**
   - Field validators dla custom logiki
   - Pydantic automatycznie waliduje typy

4. **Używaj `from_orm()` do konwersji:**
   ```python
   db_user = session.query(User).first()
   response = UserResponse.from_orm(db_user)
   ```

---

## 🔄 Różnice: Schemas vs Models

| Aspekt | Pydantic Schemas | SQLAlchemy Models |
|--------|------------------|-------------------|
| Cel | Walidacja API | Struktura bazy danych |
| Gdzie | FastAPI endpoints | Database operations |
| Walidacja | Automatyczna | Brak |
| Serializacja | JSON-ready | Wymaga konwersji |
| Relacje | Opcjonalne | Zdefiniowane w bazie |

**Przykład flow:**
```
Request → Pydantic Schema (walidacja) → SQLAlchemy Model (zapis do DB) → Pydantic Schema (response)
```

---

## 📚 Import

```python
# Wszystkie schemas dostępne z app.schemas
from app.schemas import (
    UserCreate,
    ProductResponse,
    OrderCreate,
    Token,
    # ...
)
```
