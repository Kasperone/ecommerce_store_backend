#!/usr/bin/env python3

import os
import sys
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from dotenv import load_dotenv

# Add the app directory to the path
sys.path.append(os.path.join(os.path.dirname(__file__), 'app'))

from app.models.user import User

# Load environment variables
load_dotenv()

def list_users():
    # Get database URL from environment
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL not found in environment variables")
        return
    
    try:
        # Create database connection
        engine = create_engine(database_url)
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        db = SessionLocal()
        
        # Query all users
        users = db.query(User).all()
        
        if not users:
            print("No users found in the database.")
        else:
            print(f"Found {len(users)} registered users:")
            print("-" * 80)
            
            for user in users:
                print(f"ID: {user.id}")
                print(f"Email: {user.email}")
                print(f"Name: {user.first_name} {user.last_name}")
                print(f"Phone: {user.phone or 'Not provided'}")
                print(f"Role: {user.role}")
                print(f"Active: {user.is_active}")
                print(f"Verified: {user.is_verified}")
                print(f"Created: {user.created_at}")
                
                if user.shipping_street:
                    print(f"Shipping Address: {user.shipping_street}, {user.shipping_city}, {user.shipping_postal_code}")
                
                if user.company_name:
                    print(f"Company: {user.company_name} (Tax ID: {user.company_tax_id})")
                
                print("-" * 80)
                
    except Exception as e:
        print(f"ERROR: Failed to connect to database or query users: {e}")
    finally:
        if 'db' in locals():
            db.close()

if __name__ == "__main__":
    list_users()