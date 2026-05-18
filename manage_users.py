#!/usr/bin/env python3
"""Delete a user by email and create a new one. Interactive password prompt."""

import getpass
import os
import sys

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.append(os.path.join(os.path.dirname(__file__), "app"))

from app.core.security import get_password_hash
from app.models import User
from app.models.user import UserRole

load_dotenv()


def main():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL not set")
        return 1

    engine = create_engine(database_url)
    Session = sessionmaker(bind=engine)
    db = Session()

    try:
        delete_email = input("Email of user to DELETE (blank to skip): ").strip()
        if delete_email:
            existing = db.query(User).filter(User.email == delete_email).first()
            if existing:
                db.delete(existing)
                db.commit()
                print(f"Deleted user: {delete_email}")
            else:
                print(f"No user with email {delete_email}")

        print("\n--- Create new user ---")
        email = input("Email: ").strip()
        first_name = input("First name: ").strip()
        last_name = input("Last name: ").strip()
        role_input = input("Role [ADMIN/CUSTOMER] (default ADMIN): ").strip().upper() or "ADMIN"
        password = getpass.getpass("Password: ")
        password_confirm = getpass.getpass("Confirm password: ")

        if password != password_confirm:
            print("ERROR: passwords do not match")
            return 1
        if not password:
            print("ERROR: password cannot be empty")
            return 1

        role = UserRole.ADMIN if role_input == "ADMIN" else UserRole.CUSTOMER

        new_user = User(
            email=email,
            hashed_password=get_password_hash(password),
            first_name=first_name,
            last_name=last_name,
            role=role,
            is_active=True,
            is_verified=True,
        )
        db.add(new_user)
        db.commit()
        db.refresh(new_user)
        print(
            f"\nCreated user id={new_user.id} email={new_user.email} "
            f"role={new_user.role.value} verified={new_user.is_verified}"
        )
        return 0
    except Exception as e:
        db.rollback()
        print(f"ERROR: {e}")
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
