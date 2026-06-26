"""Integration tests — exercise the FastAPI app end-to-end.

These tests boot the real app via `TestClient`. Tests that need a real DB
should use the `integration` marker and a docker-compose Postgres + Supabase.
"""
