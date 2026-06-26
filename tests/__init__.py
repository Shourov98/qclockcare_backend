"""Test suite.

Layout:
    tests/
        conftest.py             # shared fixtures (client, settings override)
        unit/                   # pure logic, no I/O
        integration/            # exercise the FastAPI app + (mocked) DB
"""
