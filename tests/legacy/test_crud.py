"""
Simple test script to verify CRUD operations work correctly
Run: python test_crud.py
"""
from sqlalchemy.orm import Session
from sqlalchemy import text
from app.core.database import SessionLocal, engine
from app.models import Base, User, Product, Category
from app.crud import user, product, category
from app.schemas.user import UserCreate
from app.schemas.product import ProductCreate
from app.schemas.category import CategoryCreate
from app.core.security import verify_password


def test_database_connection():
    """Test 1: Database connection"""
    print("\n🧪 Test 1: Database connection")
    try:
        db = SessionLocal()
        # Simple query to test connection
        db.execute(text("SELECT 1"))
        db.close()
        print("✅ Database connection successful")
        return True
    except Exception as e:
        print(f"❌ Database connection failed: {e}")
        return False


def test_user_crud(db: Session):
    """Test 2: User CRUD operations"""
    print("\n🧪 Test 2: User CRUD")
    
    try:
        # Create user
        user_data = UserCreate(
            email="test@example.com",
            password="Test1234",
            first_name="Test",
            last_name="User"
        )
        
        # Check if user exists and delete
        existing = user.get_by_email(db, email=user_data.email)
        if existing:
            user.delete(db, id=existing.id)
            print("   Cleaned up existing test user")
        
        # Create new user
        new_user = user.create(db, obj_in=user_data)
        print(f"✅ User created: {new_user.email} (ID: {new_user.id})")
        
        # Test password hashing
        assert new_user.hashed_password != "Test1234", "Password not hashed!"
        print("✅ Password hashed correctly")
        
        # Test get by email
        found_user = user.get_by_email(db, email=user_data.email)
        assert found_user is not None, "User not found by email"
        assert found_user.id == new_user.id, "Wrong user returned"
        print("✅ Get by email works")
        
        # Test authentication
        auth_user = user.authenticate(db, email=user_data.email, password="Test1234")
        assert auth_user is not None, "Authentication failed"
        assert auth_user.id == new_user.id, "Wrong user authenticated"
        print("✅ Authentication works")
        
        # Test wrong password
        wrong_auth = user.authenticate(db, email=user_data.email, password="WrongPass")
        assert wrong_auth is None, "Authentication should fail with wrong password"
        print("✅ Wrong password rejected")
        
        # Cleanup
        user.delete(db, id=new_user.id)
        print("✅ User deleted (cleanup)")
        
        return True
    except Exception as e:
        print(f"❌ User CRUD test failed: {e}")
        return False


def test_category_crud(db: Session):
    """Test 3: Category CRUD operations"""
    print("\n🧪 Test 3: Category CRUD")
    
    try:
        # Create category
        cat_data = CategoryCreate(
            name="Test Category",
            slug="test-category",
            description="Test description",
            is_active=True
        )
        
        # Cleanup existing
        existing = category.get_by_slug(db, slug=cat_data.slug)
        if existing:
            category.delete(db, id=existing.id)
        
        # Create
        new_cat = category.create(db, obj_in=cat_data)
        print(f"✅ Category created: {new_cat.name} (ID: {new_cat.id})")
        
        # Test get by slug
        found_cat = category.get_by_slug(db, slug=cat_data.slug)
        assert found_cat is not None, "Category not found by slug"
        assert found_cat.id == new_cat.id, "Wrong category returned"
        print("✅ Get by slug works")
        
        # Test get active
        active_cats = category.get_active(db)
        assert any(c.id == new_cat.id for c in active_cats), "Category not in active list"
        print("✅ Get active categories works")
        
        # Cleanup
        category.delete(db, id=new_cat.id)
        print("✅ Category deleted (cleanup)")
        
        return True
    except Exception as e:
        print(f"❌ Category CRUD test failed: {e}")
        return False


def test_product_crud(db: Session):
    """Test 4: Product CRUD operations"""
    print("\n🧪 Test 4: Product CRUD")
    
    try:
        # First create a category
        cat_data = CategoryCreate(
            name="Electronics",
            slug="electronics-test",
            is_active=True
        )
        existing_cat = category.get_by_slug(db, slug=cat_data.slug)
        if existing_cat:
            test_cat = existing_cat
        else:
            test_cat = category.create(db, obj_in=cat_data)
        
        # Create product
        prod_data = ProductCreate(
            name="Test Product",
            slug="test-product",
            sku="TEST-001",
            price_usd=99.99,
            price_pln=399.99,
            price_eur=89.99,
            stock=10,
            is_active=True,
            is_featured=True,
            images=["https://example.com/image.jpg"],
            category_id=test_cat.id
        )
        
        # Cleanup existing
        existing = product.get_by_slug(db, slug=prod_data.slug)
        if existing:
            product.delete(db, id=existing.id)
        
        # Create
        new_prod = product.create(db, obj_in=prod_data)
        print(f"✅ Product created: {new_prod.name} (ID: {new_prod.id})")
        
        # Test get by slug
        found_prod = product.get_by_slug(db, slug=prod_data.slug)
        assert found_prod is not None, "Product not found by slug"
        assert found_prod.id == new_prod.id, "Wrong product returned"
        print("✅ Get by slug works")
        
        # Test get by SKU
        found_by_sku = product.get_by_sku(db, sku=prod_data.sku)
        assert found_by_sku is not None, "Product not found by SKU"
        assert found_by_sku.id == new_prod.id, "Wrong product by SKU"
        print("✅ Get by SKU works")
        
        # Test filtering
        filtered = product.get_multi_with_filters(
            db,
            category_id=test_cat.id,
            in_stock=True
        )
        assert any(p.id == new_prod.id for p in filtered), "Product not in filtered list"
        print("✅ Filtering by category and stock works")
        
        # Test price range filtering
        price_filtered = product.get_multi_with_filters(
            db,
            min_price_usd=50.0,
            max_price_usd=150.0
        )
        assert any(p.id == new_prod.id for p in price_filtered), "Product not in price range"
        print("✅ Price range filtering works")
        
        # Test search
        search_results = product.get_multi_with_filters(
            db,
            search="Test"
        )
        assert any(p.id == new_prod.id for p in search_results), "Product not found in search"
        print("✅ Search works")
        
        # Test featured products
        featured = product.get_featured(db)
        assert any(p.id == new_prod.id for p in featured), "Product not in featured list"
        print("✅ Get featured products works")
        
        # Cleanup
        product.delete(db, id=new_prod.id)
        category.delete(db, id=test_cat.id)
        print("✅ Product and category deleted (cleanup)")
        
        return True
    except Exception as e:
        print(f"❌ Product CRUD test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    """Run all tests"""
    print("=" * 60)
    print("🚀 Starting CRUD Tests")
    print("=" * 60)
    
    results = []
    
    # Test 1: Database connection
    results.append(test_database_connection())
    
    if not results[0]:
        print("\n❌ Database connection failed. Cannot continue tests.")
        return
    
    # Create database session for other tests
    db = SessionLocal()
    
    try:
        # Test 2: User CRUD
        results.append(test_user_crud(db))
        
        # Test 3: Category CRUD
        results.append(test_category_crud(db))
        
        # Test 4: Product CRUD
        results.append(test_product_crud(db))
        
    finally:
        db.close()
    
    # Summary
    print("\n" + "=" * 60)
    print("📊 Test Summary")
    print("=" * 60)
    
    test_names = [
        "Database Connection",
        "User CRUD",
        "Category CRUD",
        "Product CRUD"
    ]
    
    for i, (name, result) in enumerate(zip(test_names, results), 1):
        status = "✅ PASSED" if result else "❌ FAILED"
        print(f"{i}. {name}: {status}")
    
    total = len(results)
    passed = sum(results)
    
    print("\n" + "=" * 60)
    print(f"Total: {passed}/{total} tests passed")
    
    if passed == total:
        print("🎉 All tests passed!")
    else:
        print(f"⚠️  {total - passed} test(s) failed")
    
    print("=" * 60)


if __name__ == "__main__":
    main()
