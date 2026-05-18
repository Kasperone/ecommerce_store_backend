from typing import Any, Generic, TypeVar

from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.models.base import BaseModel as DBBaseModel

ModelType = TypeVar("ModelType", bound=DBBaseModel)
CreateSchemaType = TypeVar("CreateSchemaType", bound=BaseModel)
UpdateSchemaType = TypeVar("UpdateSchemaType", bound=BaseModel)


class CRUDBase(Generic[ModelType, CreateSchemaType, UpdateSchemaType]):
    """Base class for CRUD operations"""

    def __init__(self, model: type[ModelType]):
        """
        CRUD object with default methods to Create, Read, Update, Delete (CRUD).

        Args:
            model: A SQLAlchemy model class
        """
        self.model = model

    def get(self, db: Session, id: Any) -> ModelType | None:
        """Get a single record by ID"""
        return db.query(self.model).filter(self.model.id == id).first()

    def get_multi(self, db: Session, *, skip: int = 0, limit: int = 100) -> list[ModelType]:
        """Get multiple records with pagination"""
        return db.query(self.model).offset(skip).limit(limit).all()

    def create(self, db: Session, *, obj_in: CreateSchemaType) -> ModelType:
        """Stage a new record and return it with ``id`` populated.

        Flushes so callers can read the autoincrement ``id`` (needed by the
        audit writer to populate ``resource_id``). Does **not** commit —
        the caller owns the transaction boundary so the mutation and any
        audit row land together. Forgetting to commit means the row is
        never persisted, which is a loud failure (no DB write), not a
        silent one.
        """
        obj_in_data = jsonable_encoder(obj_in)
        db_obj = self.model(**obj_in_data)
        db.add(db_obj)
        db.flush()
        db.refresh(db_obj)
        return db_obj

    def update(
        self, db: Session, *, db_obj: ModelType, obj_in: UpdateSchemaType | dict[str, Any]
    ) -> ModelType:
        """Mutate fields on ``db_obj`` without flushing.

        Deliberately does not flush: SQLAlchemy's ``get_history()`` is reset
        on flush, and the audit writer reads it between this call and the
        caller's ``db.commit()`` to compute the before/after diff.
        """
        obj_data = jsonable_encoder(db_obj)

        update_data = obj_in if isinstance(obj_in, dict) else obj_in.model_dump(exclude_unset=True)

        for field in obj_data:
            if field in update_data:
                setattr(db_obj, field, update_data[field])

        db.add(db_obj)
        return db_obj

    def delete(self, db: Session, *, id: int) -> ModelType | None:
        """Mark a record for deletion; caller commits."""
        obj = db.query(self.model).get(id)
        if obj is None:
            return None
        db.delete(obj)
        return obj

    def count(self, db: Session) -> int:
        """Count total records"""
        return db.query(self.model).count()
