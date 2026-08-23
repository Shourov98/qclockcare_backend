-- Manual migrations 0024, 0025, 0026 for Render production database.
-- Run with: psql "$DATABASE_URL" -f manual_migrations_0024_0025_0026.sql
--
-- Idempotent: every CREATE uses IF NOT EXISTS so re-running is safe.

BEGIN;

-- =========================================================================
-- 0024_admin_scopes — table that auth_service._load_user_scopes() queries
-- =========================================================================
CREATE TABLE IF NOT EXISTS admin_scopes (
    user_id UUID NOT NULL,
    scope_name VARCHAR(64) NOT NULL,
    granted_by UUID,
    granted_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    CONSTRAINT pk_admin_scopes PRIMARY KEY (user_id, scope_name),
    CONSTRAINT chk_admin_scopes_scope_name CHECK (
        scope_name IN ('AGENCIES', 'CLINICAL', 'SUPPORT')
    ),
    CONSTRAINT fk_admin_scopes_user_id_users FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE,
    CONSTRAINT fk_admin_scopes_granted_by_users FOREIGN KEY (granted_by)
        REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_admin_scopes_scope_name ON admin_scopes(scope_name);

-- =========================================================================
-- 0025_tickets — tickets + ticket_comments + ticket_code_sequence + 3 enums
-- =========================================================================
DO $$ BEGIN
    CREATE TYPE ticket_status AS ENUM ('OPEN', 'IN_PROGRESS', 'PENDING', 'RESOLVED', 'CLOSED');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    CREATE TYPE ticket_priority AS ENUM ('CRITICAL', 'HIGH', 'MEDIUM', 'LOW');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    CREATE TYPE ticket_comment_kind AS ENUM ('COMMENT', 'STATUS_CHANGE', 'ASSIGNMENT', 'ATTACHMENT');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS tickets (
    id UUID NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    code VARCHAR(32) NOT NULL,
    title VARCHAR(500) NOT NULL,
    description TEXT NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'OPEN',
    priority VARCHAR(32) NOT NULL DEFAULT 'MEDIUM',
    agency_id UUID,
    reporter_user_id UUID NOT NULL,
    assignee_user_id UUID,
    deleted_at TIMESTAMP WITH TIME ZONE,
    extra JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT pk_tickets PRIMARY KEY (id),
    CONSTRAINT uq_tickets_code UNIQUE (code),
    CONSTRAINT fk_tickets_agency_id_agencies FOREIGN KEY (agency_id)
        REFERENCES agencies(id) ON DELETE SET NULL,
    CONSTRAINT fk_tickets_reporter_user_id_users FOREIGN KEY (reporter_user_id)
        REFERENCES users(id) ON DELETE RESTRICT,
    CONSTRAINT fk_tickets_assignee_user_id_users FOREIGN KEY (assignee_user_id)
        REFERENCES users(id) ON DELETE SET NULL
);

DO $$ BEGIN
    ALTER TABLE tickets ALTER COLUMN status TYPE ticket_status USING status::ticket_status;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    ALTER TABLE tickets ALTER COLUMN priority TYPE ticket_priority USING priority::ticket_priority;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE INDEX IF NOT EXISTS ix_tickets_status ON tickets(status);
CREATE INDEX IF NOT EXISTS ix_tickets_priority ON tickets(priority);
CREATE INDEX IF NOT EXISTS ix_tickets_agency_id ON tickets(agency_id);
CREATE INDEX IF NOT EXISTS ix_tickets_reporter_user_id ON tickets(reporter_user_id);
CREATE INDEX IF NOT EXISTS ix_tickets_assignee_user_id ON tickets(assignee_user_id);
CREATE INDEX IF NOT EXISTS idx_tickets_status_priority ON tickets(status, priority);
CREATE INDEX IF NOT EXISTS idx_tickets_created_at ON tickets(created_at);

CREATE TABLE IF NOT EXISTS ticket_comments (
    id UUID NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    ticket_id UUID NOT NULL,
    author_user_id UUID NOT NULL,
    kind VARCHAR(32) NOT NULL DEFAULT 'COMMENT',
    body TEXT NOT NULL,
    event_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    edited_at TIMESTAMP WITH TIME ZONE,
    CONSTRAINT pk_ticket_comments PRIMARY KEY (id),
    CONSTRAINT fk_ticket_comments_ticket_id_tickets FOREIGN KEY (ticket_id)
        REFERENCES tickets(id) ON DELETE CASCADE,
    CONSTRAINT fk_ticket_comments_author_user_id_users FOREIGN KEY (author_user_id)
        REFERENCES users(id) ON DELETE RESTRICT
);

DO $$ BEGIN
    ALTER TABLE ticket_comments ALTER COLUMN kind TYPE ticket_comment_kind USING kind::ticket_comment_kind;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE INDEX IF NOT EXISTS ix_ticket_comments_ticket_id ON ticket_comments(ticket_id);
CREATE INDEX IF NOT EXISTS idx_ticket_comments_ticket_created ON ticket_comments(ticket_id, created_at);

CREATE TABLE IF NOT EXISTS ticket_code_sequence (
    seq_date DATE NOT NULL,
    last_value INTEGER NOT NULL DEFAULT 0,
    CONSTRAINT pk_ticket_code_sequence PRIMARY KEY (seq_date)
);

-- =========================================================================
-- 0026_compliance — agency_documents + agency_licenses + 3 enums
-- =========================================================================
DO $$ BEGIN
    CREATE TYPE document_type AS ENUM ('LICENSE', 'CERTIFICATE', 'DOCUMENT', 'PERMIT', 'POLICY', 'REPORT');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    CREATE TYPE document_status AS ENUM ('MISSING', 'PENDING', 'VALID', 'EXPIRING', 'EXPIRED', 'REJECTED');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    CREATE TYPE license_status AS ENUM ('VALID', 'UPCOMING', 'WARNING', 'CRITICAL', 'EXPIRED');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS agency_documents (
    id UUID NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    deleted_at TIMESTAMP WITH TIME ZONE,
    agency_id UUID NOT NULL,
    name VARCHAR(255) NOT NULL,
    doc_type VARCHAR(32) NOT NULL DEFAULT 'DOCUMENT',
    status VARCHAR(32) NOT NULL DEFAULT 'MISSING',
    description TEXT,
    expires_at TIMESTAMP WITH TIME ZONE,
    file_url VARCHAR(1024),
    extra JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT pk_agency_documents PRIMARY KEY (id),
    CONSTRAINT fk_agency_documents_agency_id_agencies FOREIGN KEY (agency_id)
        REFERENCES agencies(id) ON DELETE CASCADE
);

DO $$ BEGIN
    ALTER TABLE agency_documents ALTER COLUMN doc_type TYPE document_type USING doc_type::document_type;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    ALTER TABLE agency_documents ALTER COLUMN status TYPE document_status USING status::document_status;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE INDEX IF NOT EXISTS ix_agency_documents_agency_id ON agency_documents(agency_id);
CREATE INDEX IF NOT EXISTS ix_agency_documents_doc_type ON agency_documents(doc_type);
CREATE INDEX IF NOT EXISTS ix_agency_documents_status ON agency_documents(status);
CREATE INDEX IF NOT EXISTS ix_agency_documents_expires_at ON agency_documents(expires_at);
CREATE INDEX IF NOT EXISTS idx_agency_documents_agency_status ON agency_documents(agency_id, status);

CREATE TABLE IF NOT EXISTS agency_licenses (
    id UUID NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    deleted_at TIMESTAMP WITH TIME ZONE,
    agency_id UUID NOT NULL,
    name VARCHAR(255) NOT NULL,
    doc_type VARCHAR(32) NOT NULL DEFAULT 'LICENSE',
    status VARCHAR(32) NOT NULL DEFAULT 'VALID',
    issued_at TIMESTAMP WITH TIME ZONE,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    reference_number VARCHAR(128),
    notes TEXT,
    extra JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT pk_agency_licenses PRIMARY KEY (id),
    CONSTRAINT fk_agency_licenses_agency_id_agencies FOREIGN KEY (agency_id)
        REFERENCES agencies(id) ON DELETE CASCADE
);

DO $$ BEGIN
    ALTER TABLE agency_licenses ALTER COLUMN doc_type TYPE document_type USING doc_type::document_type;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    ALTER TABLE agency_licenses ALTER COLUMN status TYPE license_status USING status::license_status;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE INDEX IF NOT EXISTS ix_agency_licenses_agency_id ON agency_licenses(agency_id);
CREATE INDEX IF NOT EXISTS ix_agency_licenses_doc_type ON agency_licenses(doc_type);
CREATE INDEX IF NOT EXISTS ix_agency_licenses_status ON agency_licenses(status);
CREATE INDEX IF NOT EXISTS ix_agency_licenses_expires_at ON agency_licenses(expires_at);
CREATE INDEX IF NOT EXISTS idx_agency_licenses_agency_status ON agency_licenses(agency_id, status);
CREATE INDEX IF NOT EXISTS idx_agency_licenses_status_expires ON agency_licenses(status, expires_at);

-- =========================================================================
-- Tell Alembic we're at head so it doesn't try to re-run anything
-- =========================================================================
UPDATE alembic_version SET version_num = '0026_compliance';

COMMIT;

-- =========================================================================
-- Verification (run after the script returns)
-- =========================================================================
-- SELECT table_name FROM information_schema.tables
-- WHERE table_schema = 'public'
--   AND table_name IN ('admin_scopes', 'tickets', 'ticket_comments',
--                      'agency_documents', 'agency_licenses');
-- Expected: 5 rows.
--
-- SELECT * FROM alembic_version;
-- Expected: 0026_compliance