# QlockCare Backend

Backend for QlockCare — a SaaS for managing home and community-based care programs
(PCA / CFSS, 245D, ARMHS, Counseling) used by healthcare waiver agencies.

> **Status:** Phase 1 scaffold. Foundation in place; business modules under construction.

---

## Quick start (5 minutes)

```bash
# 1. Install uv (if not already)
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Install dependencies
uv sync --extra dev

# 3. Copy env
cp .env.example .env

# 4. Start dependencies (Postgres via Supabase, MailHog for SMTP, Floci for S3)
supabase start
floci start
docker run --rm -d -p 1025:1025 -p 8025:8025 --name mailhog mailhog/mailhog

# 5. Run migrations
uv run alembic upgrade head

# 6. Seed dev data (optional)
uv run python -m scripts.seed_dev

# 7. Start the API
uv run uvicorn src.main:app --reload --port 8000

# 8. Verify
curl http://localhost:8000/health
open http://localhost:8000/docs
open http://localhost:8025        # MailHog (captured emails)
aws --endpoint-url http://localhost:4566 s3 ls s3://qualifications --recursive
```

---

## Project layout

```
src/
├── main.py                  # FastAPI app factory
├── core/                    # cross-cutting infrastructure
│   ├── config.py            # pydantic-settings (env-driven)
│   ├── exceptions.py        # AppException base + global handler
│   ├── logging.py           # structlog config
│   ├── middleware.py        # request_id, correlation
│   ├── database.py          # async engine + session factory
│   └── health.py            # /health, /ready
├── shared/                  # utilities reused across modules
│   ├── domain/              # base entities, value objects, mixins
│   ├── repositories/        # base repository protocol
│   ├── schemas/             # response envelope, pagination
│   ├── storage/             # S3-compatible file storage (ADR-0018)
│   └── utils/               # datetime, ids, etc.
└── modules/                 # business modules (auth, users, agencies, ...)
tests/
├── unit/                    # pure unit tests
└── integration/             # tests that hit DB / HTTP
alembic/                     # migrations
scripts/                     # seed, maintenance
```

See `docs/09_BACKEND_STRUCTURE_SOLID_OOP.md` for the full module design.

---

## Daily commands

```bash
# Run the API
uv run uvicorn src.main:app --reload

# Run all tests
uv run pytest

# Lint + format
uv run ruff check --fix .
uv run ruff format .

# Type-check
uv run mypy src

# Create a new migration
uv run alembic revision -m "add notifications"

# Apply migrations
uv run alembic upgrade head

# Roll back one migration
uv run alembic downgrade -1

# Inspect Floci (local S3)
aws --endpoint-url http://localhost:4566 s3 ls s3://qualifications --recursive

# Tail Floci logs
floci logs
```

---

## Documentation

All design docs live in `qclockcare_backend_docs/` (sibling directory):

- `01`–`08` — original planning docs
- `09` — backend structure (SOLID / OOP / 7-file pattern)
- `10` — feature implementation checklist (244 items)
- `11` — API reference
- `12` — Postman collection
- `13` — database schema
- `14` — RLS / multi-tenancy
- `15` — pagination & filtering
- `16` — env vars & secrets
- `17` — seeding & demo data
- `18` — error code mapping
- `19` — service split example
- `20` — CI/CD & observability
- `21` — development guide
- `22` — architecture decision records
- `23` — operational runbooks
- `24` — Git workflow
- `25` — auth & hosting decisions
- `26` — local storage (Floci)

---

## Architecture decisions

| ADR | Title |
|-----|-------|
| 001 | Multi-Tenancy Model |
| 002 | Soft Delete Strategy |
| 003 | State Machine Implementation |
| 004 | RBAC Layering |
| 005 | RLS-First Security |
| 006 | JWT Algorithm Choice |
| 007 | Notification Channel Abstraction |
| 008 | Service Split Pattern |
| 009 | Append-Only Audit Log |
| 010 | Testing Strategy |
| 011 | Idempotency on State-Changing |
| 012 | Time Zone Handling |
| 013 | Error Response Shape |
| 014 | Repository Pattern |
| 015 | OpenAPI as Source of Truth |
| 016 | Email Verification with 4-Digit OTP |
| 017 | Database Hosting on Supabase |
| 018 | S3-Compatible Storage with Floci |

---

## License

Proprietary — QlockCare Inc. All rights reserved.