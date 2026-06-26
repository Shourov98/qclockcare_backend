"""Base repository protocol.

All repositories implement this minimal CRUD surface; specific repositories
extend it with domain-specific queries (per module).
"""

from src.shared.repositories.base_repository import BaseRepository

__all__ = ["BaseRepository"]
