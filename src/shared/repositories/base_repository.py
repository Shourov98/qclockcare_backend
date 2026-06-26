"""Generic repository protocol + abstract base.

Every concrete repository follows the same shape:
    get(id) -> T | None
    list(...) -> list[T]
    add(entity) -> T
    update(entity) -> T
    delete(entity) -> None

Module-specific repositories add domain queries on top.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Generic, Protocol, TypeVar, runtime_checkable
from uuid import UUID

from sqlalchemy import delete, select

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@runtime_checkable
class _HasId(Protocol):
    """Anything used as a `T` for BaseRepository must have an `id` attribute."""

    id: Any


T = TypeVar("T", bound=_HasId)


class BaseRepository(ABC, Generic[T]):
    """Minimal CRUD surface every repository implements.

    `model_class` is the SQLAlchemy ORM class the repo works with. Concrete
    repositories pass it via the constructor. `T` is bound to a Protocol
    requiring `.id` so attribute access on `self._model` type-checks.
    """

    def __init__(self, session: AsyncSession, model_class: type[T]) -> None:
        self._session = session
        self._model = model_class

    # ---- reads ----
    async def get(self, entity_id: UUID) -> T | None:
        return await self._session.get(self._model, entity_id)

    async def get_or_none(self, entity_id: UUID) -> T | None:
        result = await self._session.execute(
            select(self._model).where(self._model.id == entity_id),
        )
        return result.scalar_one_or_none()

    async def list_all(self, *, limit: int = 100, offset: int = 0) -> list[T]:
        result = await self._session.execute(
            select(self._model).limit(limit).offset(offset),
        )
        return list(result.scalars().all())

    async def count(self, **filters: Any) -> int:
        from sqlalchemy import func as sa_func

        stmt = select(sa_func.count()).select_from(self._model)
        for key, value in filters.items():
            stmt = stmt.where(getattr(self._model, key) == value)
        result = await self._session.execute(stmt)
        return int(result.scalar_one())

    # ---- writes ----
    async def add(self, entity: T) -> T:
        self._session.add(entity)
        await self._session.flush()
        return entity

    async def add_all(self, entities: list[T]) -> list[T]:
        self._session.add_all(entities)
        await self._session.flush()
        return entities

    async def delete_by_id(self, entity_id: UUID) -> bool:
        stmt = delete(self._model).where(self._model.id == entity_id)
        result = await self._session.execute(stmt)
        rowcount: int = getattr(result, "rowcount", 0) or 0
        return rowcount > 0

    # ---- internals exposed for subclasses ----
    @property
    def session(self) -> AsyncSession:
        return self._session

    @property
    def model(self) -> type[T]:
        return self._model

    @abstractmethod
    def __repr__(self) -> str:
        return f"<{type(self).__name__} model={self._model.__name__}>"
