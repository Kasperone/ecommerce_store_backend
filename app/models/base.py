from datetime import UTC, datetime

from sqlalchemy import Column, DateTime, Integer
from sqlalchemy.ext.declarative import declarative_base

Base = declarative_base()


def _utcnow() -> datetime:
    return datetime.now(UTC)


class TimestampMixin:
    """Mixin that adds timestamp fields to models"""

    created_at = Column(DateTime, default=_utcnow, nullable=False)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow, nullable=False)


class BaseModel(Base, TimestampMixin):
    """Base model with id and timestamps"""

    __abstract__ = True

    id = Column(Integer, primary_key=True, index=True)
