"""Shared test fixtures for the FX engine test suite."""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

# Ensure ``src`` package is importable from the repo root.
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.database import reset_db  # noqa: E402
from src.engine import FXEngine, RateProvider  # noqa: E402

TEST_DB_PATH = Path(__file__).parent / "test_fx.db"


@pytest.fixture(autouse=True)
def fresh_db():
    """Reset the database before every test function."""
    reset_db(TEST_DB_PATH)
    yield
    if TEST_DB_PATH.exists():
        TEST_DB_PATH.unlink()


@pytest.fixture
def rate_provider():
    return RateProvider()


@pytest.fixture
def engine(rate_provider):
    return FXEngine(rate_provider, db_path=TEST_DB_PATH)


@pytest.fixture
def customer_with_balance(engine):
    """Create a customer pre-funded in all four currencies."""
    result = engine.create_customer("Test User")
    cid = result["customer_id"]
    engine.credit_balance(cid, "USD", Decimal("10000.00"))
    engine.credit_balance(cid, "EUR", Decimal("10000.00"))
    engine.credit_balance(cid, "KES", Decimal("1000000.00"))
    engine.credit_balance(cid, "NGN", Decimal("10000000.00"))
    return cid
