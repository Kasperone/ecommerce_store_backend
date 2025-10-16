"""
Test updated dependencies - schemas, security, imports
Run: python test_updated_dependencies.py
"""
import sys
from datetime import timedelta


def test_imports():
    """Test 1: Import all major packages"""
    print("\n🧪 Test 1: Package Imports")
    
    packages = [
        ("fastapi", "FastAPI, Depends, HTTPException"),
        ("pydantic", "BaseModel, Field, validator"),
        ("sqlalchemy", "Column, Integer, String, create_engine"),
        ("alembic", "config, command"),
        ("uvicorn", "run, Config"),
        ("jose", "jwt"),
        ("passlib.context", "CryptContext"),
        ("boto3", "client, resource"),
    ]
    
    failed = []
    for package, components in packages:
        try:
            __import__(package)
            print(f"✅ {package}")
        except ImportError as e:
            print(f"❌ {package}: {e}")
            failed.append(package)
    
    if failed:
        print(f"\n❌ Failed imports: {', '.join(failed)}")
        return False
    
    print("✅ All packages imported successfully")
    return True


def test_pydantic_schemas():
    """Test 2: Pydantic schemas validation"""
    print("\n🧪 Test 2: Pydantic Schemas Validation")
    
    try:
        from app.schemas import (
            UserCreate, UserResponse, UserUpdate,
            ProductCreate, ProductResponse, ProductUpdate,
            CategoryCreate, CategoryResponse,
            Token
        )
        print("✅ All schemas imported")
        
        # Test UserCreate validation
        try:
            user = UserCreate(
                email="test@test.com",
                password="Test1234",
                first_name="John"
            )
            assert user.email == "test@test.com"
            print("✅ UserCreate validation works")
        except Exception as e:
            print(f"❌ UserCreate validation failed: {e}")
            return False
        
        # Test password validation (too short)
        try:
            UserCreate(email="test@test.com", password="short")
            print("❌ Password validation should fail for short password")
            return False
        except Exception:
            print("✅ Password validation (min length) works")
        
        # Test password validation (no uppercase)
        try:
            UserCreate(email="test@test.com", password="test1234")
            print("❌ Password validation should fail without uppercase")
            return False
        except Exception:
            print("✅ Password validation (uppercase required) works")
        
        # Test ProductCreate validation
        try:
            product = ProductCreate(
                name="Test Product",
                slug="test-product",
                price_usd=99.99,
                price_pln=399.99,
                price_eur=89.99,
                stock=10,
                images=["https://example.com/img.jpg"]
            )
            assert product.price_usd == 99.99
            print("✅ ProductCreate validation works")
        except Exception as e:
            print(f"❌ ProductCreate validation failed: {e}")
            return False
        
        # Test price validation (negative price)
        try:
            ProductCreate(
                name="Test",
                slug="test",
                price_usd=-10.0,
                price_pln=100,
                price_eur=100
            )
            print("❌ Price validation should fail for negative price")
            return False
        except Exception:
            print("✅ Price validation (positive only) works")
        
        # Test CategoryCreate
        try:
            category = CategoryCreate(
                name="Electronics",
                slug="electronics"
            )
            assert category.name == "Electronics"
            print("✅ CategoryCreate validation works")
        except Exception as e:
            print(f"❌ CategoryCreate validation failed: {e}")
            return False
        
        return True
        
    except Exception as e:
        print(f"❌ Schema tests failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_security():
    """Test 3: Security functions (JWT, password hashing)"""
    print("\n🧪 Test 3: Security Functions")
    
    try:
        from app.core.security import (
            get_password_hash,
            verify_password,
            create_access_token
        )
        
        # Test password hashing
        password = "SecurePass123"
        hashed = get_password_hash(password)
        
        assert hashed != password, "Password not hashed"
        assert hashed.startswith("$2b$"), "Not bcrypt hash"
        print("✅ Password hashing works")
        
        # Test password verification
        assert verify_password(password, hashed), "Password verification failed"
        print("✅ Password verification works")
        
        # Test wrong password
        assert not verify_password("WrongPass", hashed), "Wrong password accepted"
        print("✅ Wrong password rejected")
        
        # Test JWT token creation
        token = create_access_token(subject="123")
        assert token is not None, "Token not created"
        assert len(token) > 50, "Token too short"
        print("✅ JWT token creation works")
        
        # Test token with custom expiration
        token_custom = create_access_token(
            subject="456",
            expires_delta=timedelta(minutes=60)
        )
        assert token_custom is not None
        print("✅ JWT token with custom expiration works")
        
        # Test token decoding
        from jose import jwt
        from app.core.config import settings
        
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=["HS256"])
        assert payload.get("sub") == "123", "Token payload incorrect"
        print("✅ JWT token decoding works")
        
        return True
        
    except Exception as e:
        print(f"❌ Security tests failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_dependencies():
    """Test 4: FastAPI dependencies"""
    print("\n🧪 Test 4: FastAPI Dependencies")
    
    try:
        from app.core.dependencies import get_db
        print("✅ get_db dependency imported")
        
        # Test database session generator
        db_gen = get_db()
        db = next(db_gen)
        assert db is not None, "Database session not created"
        print("✅ Database session created")
        
        # Test session has query method
        assert hasattr(db, 'query'), "Session missing query method"
        assert hasattr(db, 'commit'), "Session missing commit method"
        assert hasattr(db, 'rollback'), "Session missing rollback method"
        print("✅ Database session has required methods")
        
        # Close session
        try:
            next(db_gen)
        except StopIteration:
            print("✅ Database session cleanup works")
        
        return True
        
    except Exception as e:
        print(f"❌ Dependencies tests failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_models_relationships():
    """Test 5: SQLAlchemy models and relationships"""
    print("\n🧪 Test 5: SQLAlchemy Models & Relationships")
    
    try:
        from app.models import User, Product, Category, Order, OrderItem
        
        # Check User model
        assert hasattr(User, 'email'), "User missing email field"
        assert hasattr(User, 'hashed_password'), "User missing password field"
        assert hasattr(User, 'orders'), "User missing orders relationship"
        print("✅ User model structure correct")
        
        # Check Product model
        assert hasattr(Product, 'name'), "Product missing name"
        assert hasattr(Product, 'price_usd'), "Product missing price_usd"
        assert hasattr(Product, 'price_pln'), "Product missing price_pln"
        assert hasattr(Product, 'price_eur'), "Product missing price_eur"
        assert hasattr(Product, 'category'), "Product missing category relationship"
        print("✅ Product model structure correct")
        
        # Check Category model
        assert hasattr(Category, 'name'), "Category missing name"
        assert hasattr(Category, 'slug'), "Category missing slug"
        assert hasattr(Category, 'products'), "Category missing products relationship"
        print("✅ Category model structure correct")
        
        # Check Order model
        assert hasattr(Order, 'order_number'), "Order missing order_number"
        assert hasattr(Order, 'user_id'), "Order missing user_id"
        assert hasattr(Order, 'items'), "Order missing items relationship"
        print("✅ Order model structure correct")
        
        # Check OrderItem model
        assert hasattr(OrderItem, 'product_id'), "OrderItem missing product_id"
        assert hasattr(OrderItem, 'quantity'), "OrderItem missing quantity"
        print("✅ OrderItem model structure correct")
        
        return True
        
    except Exception as e:
        print(f"❌ Models tests failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_fastapi_app():
    """Test 6: FastAPI app initialization"""
    print("\n🧪 Test 6: FastAPI App Initialization")
    
    try:
        from app.main import app
        
        assert app is not None, "App not created"
        print("✅ FastAPI app created")
        
        # Check app has routes
        assert len(app.routes) > 0, "No routes registered"
        print(f"✅ App has {len(app.routes)} routes")
        
        # Check health endpoint exists
        route_paths = [route.path for route in app.routes if hasattr(route, 'path')]
        assert '/health' in route_paths, "Health endpoint missing"
        print("✅ Health endpoint exists")
        
        return True
        
    except Exception as e:
        print(f"❌ FastAPI app tests failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    """Run all tests"""
    print("=" * 60)
    print("🚀 Testing Updated Dependencies")
    print("=" * 60)
    
    results = []
    
    # Test 1: Imports
    results.append(("Package Imports", test_imports()))
    
    # Test 2: Pydantic schemas
    results.append(("Pydantic Schemas", test_pydantic_schemas()))
    
    # Test 3: Security
    results.append(("Security Functions", test_security()))
    
    # Test 4: Dependencies
    results.append(("FastAPI Dependencies", test_dependencies()))
    
    # Test 5: Models
    results.append(("SQLAlchemy Models", test_models_relationships()))
    
    # Test 6: FastAPI app
    results.append(("FastAPI App", test_fastapi_app()))
    
    # Summary
    print("\n" + "=" * 60)
    print("📊 Test Summary")
    print("=" * 60)
    
    for i, (name, result) in enumerate(results, 1):
        status = "✅ PASSED" if result else "❌ FAILED"
        print(f"{i}. {name}: {status}")
    
    total = len(results)
    passed = sum(result for _, result in results)
    
    print("\n" + "=" * 60)
    print(f"Total: {passed}/{total} tests passed")
    
    if passed == total:
        print("🎉 All dependency tests passed!")
        print("=" * 60)
        sys.exit(0)
    else:
        print(f"⚠️  {total - passed} test(s) failed")
        print("=" * 60)
        sys.exit(1)


if __name__ == "__main__":
    main()
