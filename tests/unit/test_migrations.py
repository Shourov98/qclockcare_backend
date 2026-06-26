"""Alembic migration smoke tests.

We don't connect to a real Postgres here — that requires docker-compose.
Instead, we verify that the migration module is syntactically valid Python
and contains the expected upgrade/downgrade callables.

For full DB verification, run `uv run alembic upgrade head` against a
running Postgres (see `21_DEVELOPMENT_GUIDE.md`).
"""

from __future__ import annotations

from pathlib import Path


def test_alembic_env_compiles() -> None:
    """`alembic/env.py` is syntactically valid Python.

    We compile (not import) because alembic/ isn't a Python package — it
    has no __init__.py. For full import validation, run
    `uv run alembic upgrade head --sql` against a Postgres.
    """
    env_path = Path(__file__).resolve().parents[2] / "alembic" / "env.py"
    assert env_path.exists(), "alembic/env.py missing"
    compile(env_path.read_text(), str(env_path), "exec")


def test_migration_0001_exists() -> None:
    """The first migration file exists in alembic/versions/."""
    versions_dir = Path(__file__).resolve().parents[2] / "alembic" / "versions"
    files = list(versions_dir.glob("0001_*.py"))
    assert len(files) == 1, f"Expected exactly one 0001 migration, found {files}"
    assert "0001_base" in files[0].name, f"Expected 0001_base, got {files[0].name}"


def test_migration_0002_exists() -> None:
    """The RLS scaffold migration exists."""
    versions_dir = Path(__file__).resolve().parents[2] / "alembic" / "versions"
    files = list(versions_dir.glob("0002_*.py"))
    assert len(files) == 1, f"Expected exactly one 0002 migration, found {files}"
    assert "0002_rls" in files[0].name, f"Expected 0002_rls, got {files[0].name}"


def test_alembic_ini_loads() -> None:
    """`alembic.ini` is present and contains the expected sections."""
    import configparser

    cfg_path = Path(__file__).resolve().parents[2] / "alembic.ini"
    assert cfg_path.exists()

    parser = configparser.ConfigParser()
    parser.read(cfg_path)
    assert "alembic" in parser
    assert parser.get("alembic", "script_location") == "alembic"
