# QlockCare Backend

Backend for QlockCare — a SaaS for managing home and community-based care
programs (PCA / CFSS, 245D, ARMHS, Counseling) used by healthcare waiver
agencies.

> **Status:** Phase 1 complete. All 9 business modules live (auth, agencies,
> staff, patients, guardians, appointments, visits, notifications,
> locations, audit-logs). Phase 2 features (patient portal self-service,
> change-password, bulk export, Docker) on the roadmap.

For a **plain-English overview** aimed at non-technical readers, see
[`docs/CLIENT_GUIDE.md`](docs/CLIENT_GUIDE.md).

---

## Quick start (5 minutes, hosted Supabase)

This project runs against a hosted Supabase project (Postgres + Storage
+ Auth). You'll need the 5 credentials your client provided.

```bash
# 1. Install uv (if not already)
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Install dependencies
uv sync --extra dev

# 3. Copy env + fill in the values from your client
cp .env.example .env
# Edit .env — see "Required .env values" below.

# 4. Run database migrations against the Supabase Postgres
uv run alembic upgrade head

# 5. Seed dev users (super admin + agency admin + staff + patient)
uv run python scripts/seed_test_user.py

# 6. Start the API
uv run uvicorn src.main:app --reload --port 8000

# 7. Verify
curl http://localhost:8000/ready              # → {"status":"ready", ...}
open http://localhost:8000/docs                # auto-generated Swagger UI
```

### Required `.env` values

The client email gives you 5 things. Map them onto these env vars:

| From the client email          | Set this env var                  |
| ------------------------------ | --------------------------------- |
| Project URL                    | `SUPABASE_URL`                    |
| anon key                       | `SUPABASE_ANON_KEY`               |
| service_role key               | `SUPABASE_SERVICE_ROLE_KEY`       |
| JWT secret                     | `SUPABASE_JWT_SECRET` (≥32 chars) |
| DATABASE_URL (port 6543)       | `DATABASE_URL` (use scheme `postgresql+asyncpg://`) |
| DIRECT_URL (port 5432)         | `DIRECT_URL` (same scheme)        |

The default `STORAGE_BACKEND=supabase` uses the same project for file
storage — no extra credentials needed. See "Storage" below.

### One-time bucket setup

The backend can't create buckets over the API. In the Supabase dashboard:

1. **Storage** → **New bucket** → name `qualifications`, **Public: off**, file size limit 10 MB.
2. Click **Create**.

Or run in the SQL editor:

```sql
INSERT INTO storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
VALUES ('qualifications', 'qualifications', false, 10485760,
        ARRAY['image/png', 'image/jpeg', 'application/pdf'])
ON CONFLICT (id) DO NOTHING;
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
│   ├── security.py          # password hashing (argon2id), JWT primitives
│   └── health.py            # /health, /ready
├── shared/                  # utilities reused across modules
│   ├── domain/              # base entities, value objects, enums
│   ├── repositories/        # base repository protocol
│   ├── schemas/             # response envelope, pagination, OpenAPI helpers
│   ├── storage/             # file storage adapter (Supabase + S3-compatible)
│   └── utils/               # datetime, ids, etc.
└── modules/                 # business modules
    ├── auth/                # transactional email (invitations, OTPs)
    ├── identity/            # /auth/* — login, refresh, accept-invitation, OTPs
    ├── agencies/            # tenant management (models only on develop)
    ├── staff/               # staff profiles, qualifications, availability
    ├── patients/            # patients, guardians, relationships
    ├── appointments/        # scheduling + state machine
    ├── visits/              # on-site delivery + verification
    ├── portal/              # patient-facing /portal/visits surface
    ├── notifications/       # in-app + email + SMS dispatch
    ├── locations/           # service-delivery addresses
    └── audit_logs/          # append-only action history

tests/
├── unit/                    # pure unit tests (no DB)
└── integration/             # tests against a running server + real DB

alembic/                     # SQLAlchemy migrations
docs/
├── flow/                    # Graphviz diagrams for each module's main flows
│                            # (.dot source + rendered .png + .svg)
├── CLIENT_GUIDE.md          # plain-English overview for non-technical readers
└── CLIENT_GUIDE.{docx,odt}  # same doc in Word + LibreOffice formats
postman/                     # Postman collection + Newman CI runner
scripts/
├── seed_test_user.py        # creates super admin + agency admin + staff + patient
└── run_newman.sh            # Newman runner used by .github/workflows/api-smoke.yml
```

See [`docs/flow/`](docs/flow/) for per-module workflow diagrams and
`qclockcare_backend_docs/` (sibling directory) for the full design docs.

---

## Daily commands

```bash
# Run the API
uv run uvicorn src.main:app --reload --port 8000

# Run all tests
uv run pytest

# Just unit tests (no DB required, ~2s)
uv run pytest tests/unit/ -q

# Integration tests (needs Supabase + a running server on :8001)
uv run pytest tests/integration/ -q

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

# Inspect Supabase Storage bucket (requires aws cli + Supabase S3 keys)
aws --endpoint-url https://<ref>.supabase.co/storage/v1/s3 \
  s3 ls s3://qualifications --recursive

# Tail API logs (structlog JSON to stdout)
# Tip: pipe through `jq` for readable JSON, e.g. `uv run uvicorn ... | jq 'select(.level=="error")'`
```

---

## Storage

The backend ships with **two interchangeable storage adapters**, picked
at runtime by `STORAGE_BACKEND`:

| Backend     | When to use                                       | Auth |
| ----------- | ------------------------------------------------- | ---- |
| `supabase`  | Default. Same project as the database.            | `SUPABASE_SERVICE_ROLE_KEY` |
| `s3`        | AWS S3, MinIO, Cloudflare R2, or local Floci dev. | `S3_*` env vars |

The active adapter is selected in `src/shared/storage/factory.py`; all
callers go through `get_storage()` and never touch boto3 / supabase-py
directly. Signed-URL TTL is shared by both adapters — set
`STORAGE_PRESIGNED_URL_TTL_SECONDS=900` (default).

---

## Manual API testing (Postman / Newman)

Import the collection into Postman for click-through testing:

```bash
# 1. Seed dev users
uv run python scripts/seed_test_user.py

# 2. Start the API on :8001 (Postman env points here)
uv run uvicorn src.main:app --port 8001

# 3. In Postman: File → Import →
#      postman/QlockCare_API.postman_collection.json
#      postman/environments/Local.postman_environment.json
# 4. Send auth > Login — tokens auto-populate the env.
```

The same collection runs under Newman in CI on every PR — see
`.github/workflows/api-smoke.yml`.

> **Known drift:** the collection was last regenerated before several
> modules landed. See
> [`postman/README.md`](postman/README.md#known-coverage-gaps) for the
> current list of endpoints with mismatched methods or pagination that
> needs fixing.

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

Workflow diagrams (per-module Graphviz flowcharts):
[`docs/flow/`](docs/flow/).

Plain-English overview for non-technical readers:
[`docs/CLIENT_GUIDE.md`](docs/CLIENT_GUIDE.md)
(also available as `.docx` and `.odt`).

---

## License

Proprietary — QlockCare Inc. All rights reserved.