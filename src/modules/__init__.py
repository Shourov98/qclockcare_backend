"""Feature modules (auth, agencies, clients, staff, schedules, etc.).

Each module follows the layout:

    src/modules/<feature>/
        __init__.py
        router.py          # FastAPI routes
        schemas.py         # pydantic request/response models
        service.py         # business logic
        repository.py      # DB access
        models.py          # SQLAlchemy ORM models (when added)

Modules import from `core/` and `shared/` but never from sibling modules —
cross-module references go through public services.
"""
