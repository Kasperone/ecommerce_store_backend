"""Script to delete unverified users from database"""
from sqlalchemy import create_engine, text
from app.core.config import settings

def delete_unverified_users():
    engine = create_engine(settings.DATABASE_URL)
    
    with engine.connect() as conn:
        result = conn.execute(
            text("DELETE FROM users WHERE is_verified = false")
        )
        conn.commit()
        print(f"✅ Deleted {result.rowcount} unverified user(s)")

if __name__ == "__main__":
    delete_unverified_users()
