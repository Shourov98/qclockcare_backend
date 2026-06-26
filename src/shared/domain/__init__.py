"""Domain layer — pure Python value objects, entities, mixins.

Nothing in this folder imports from SQLAlchemy or FastAPI. Domain code is
testable without any infrastructure.
"""

from src.shared.domain.base_entity import Base, IdMixin, SoftDeleteMixin, TimestampedMixin

__all__ = ["Base", "IdMixin", "SoftDeleteMixin", "TimestampedMixin"]
