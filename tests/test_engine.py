"""Core engine tests: customers, quotes, execution, idempotency,
rate resolution, and rate-staleness policies.

Each test exercises a specific invariant from SPEC.md.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from src.database import get_connection
from src.engine import FXEngine, RateProvider

# Re-use the shared TEST_DB_PATH so we can poke the DB directly.
from tests.conftest import TEST_DB_PATH


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Customer Management
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestCustomerManagement:
    def test_create_customer(self, engine):
        result = engine.create_customer("Alice")
        assert "customer_id" in result
        assert result["name"] == "Alice"

    def test_view_balances_all_zero(self, engine):
        cust = engine.create_customer("Bob")
        balances = engine.get_balances(cust["customer_id"])
        for ccy in ("USD", "EUR", "KES", "NGN"):
            assert balances["balances"][ccy] == "0.00"

    def test_credit_balance(self, engine):
        cust = engine.create_customer("Charlie")
        result = engine.credit_balance(
            cust["customer_id"], "USD", Decimal("500.00")
        )
        assert result["new_balance"] == "500.00"

    def test_credit_rejects_negative(self, engine):
        cust = engine.create_customer("Dave")
        with pytest.raises(ValueError, match="positive"):
            engine.credit_balance(
                cust["customer_id"], "USD", Decimal("-10")
            )

    def test_customer_not_found(self, engine):
        with pytest.raises(ValueError, match="not found"):
            engine.get_balances("nonexistent-id")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Quote Generation (SPEC.md §4)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestQuoteGeneration:
    def test_basic_quote(self, engine, customer_with_balance):
        quote = engine.generate_quote(
            customer_with_balance, "USD", "KES", Decimal("100")
        )
        assert quote.from_currency == "USD"
        assert quote.to_currency == "KES"
        assert quote.amount == Decimal("100")
        assert quote.final_amount > 0
        assert quote.rate > 0
        assert quote.expires_at > quote.created_at

    def test_quote_rejects_zero_amount(self, engine, customer_with_balance):
        with pytest.raises(ValueError, match="positive"):
            engine.generate_quote(
                customer_with_balance, "USD", "KES", Decimal("0")
            )

    def test_quote_rejects_same_currency(self, engine, customer_with_balance):
        with pytest.raises(ValueError, match="differ"):
            engine.generate_quote(
                customer_with_balance, "USD", "USD", Decimal("100")
            )

    def test_quote_ttl_is_60_seconds(self, engine, customer_with_balance):
        quote = engine.generate_quote(
            customer_with_balance, "USD", "EUR", Decimal("50")
        )
        delta = (quote.expires_at - quote.created_at).total_seconds()
        assert delta == 60

    def test_all_12_pairs_resolve(self, engine, customer_with_balance):
        """Every valid directional pair must produce a quote."""
        currencies = ["USD", "EUR", "KES", "NGN"]
        for src in currencies:
            for dst in currencies:
                if src == dst:
                    continue
                quote = engine.generate_quote(
                    customer_with_balance, src, dst, Decimal("1")
                )
                assert quote.final_amount > 0, f"Failed for {src}/{dst}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Quote Execution (SPEC.md §5)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestQuoteExecution:
    def test_execute_succeeds(self, engine, customer_with_balance):
        quote = engine.generate_quote(
            customer_with_balance, "USD", "KES", Decimal("100")
        )
        result = engine.execute_quote(quote.id)
        assert result["quote_id"] == quote.id
        assert result["transaction_id"]

    def test_execute_debits_and_credits(self, engine, customer_with_balance):
        """The two-leg invariant: source debited, destination credited."""
        before = engine.get_balances(customer_with_balance)
        usd_before = Decimal(before["balances"]["USD"])
        kes_before = Decimal(before["balances"]["KES"])

        quote = engine.generate_quote(
            customer_with_balance, "USD", "KES", Decimal("100")
        )
        engine.execute_quote(quote.id)

        after = engine.get_balances(customer_with_balance)
        usd_after = Decimal(after["balances"]["USD"])
        kes_after = Decimal(after["balances"]["KES"])

        assert usd_after == usd_before - Decimal("100")
        assert kes_after == kes_before + quote.final_amount

    def test_execute_uses_locked_rate(self, engine, customer_with_balance):
        """Execution must use the rate from quote creation — never
        a fresh rate (SPEC.md §4, planted_bugs Bug #2).
        """
        quote = engine.generate_quote(
            customer_with_balance, "USD", "KES", Decimal("100")
        )
        result = engine.execute_quote(quote.id)
        assert result["rate"] == str(quote.rate)
        assert result["final_amount"] == str(quote.final_amount)

    def test_execute_unknown_quote_raises(self, engine):
        with pytest.raises(ValueError, match="not found"):
            engine.execute_quote("nonexistent-id")

    def test_execute_expired_quote_raises(self, engine, customer_with_balance):
        """Quotes past their 60 s TTL must be rejected."""
        quote = engine.generate_quote(
            customer_with_balance, "USD", "EUR", Decimal("50")
        )
        # Directly set expires_at to the past in the DB
        past = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
        with get_connection(TEST_DB_PATH) as conn:
            conn.execute(
                "UPDATE quotes SET expires_at = ? WHERE id = ?",
                (past, quote.id),
            )
        with pytest.raises(ValueError, match="expired"):
            engine.execute_quote(quote.id)

    def test_double_execution_raises(self, engine, customer_with_balance):
        quote = engine.generate_quote(
            customer_with_balance, "USD", "EUR", Decimal("50")
        )
        engine.execute_quote(quote.id)
        with pytest.raises(ValueError, match="already executed"):
            engine.execute_quote(quote.id)

    def test_insufficient_balance_rolls_back(self, engine):
        """If the source balance is too low, both balances must remain
        unchanged (atomic rollback — SPEC.md §5).
        """
        cust = engine.create_customer("Broke User")
        cid = cust["customer_id"]
        engine.credit_balance(cid, "USD", Decimal("10.00"))

        quote = engine.generate_quote(cid, "USD", "KES", Decimal("10000"))
        with pytest.raises(ValueError, match="insufficient"):
            engine.execute_quote(quote.id)

        # Both balances must be untouched
        balances = engine.get_balances(cid)
        assert balances["balances"]["USD"] == "10.00"
        assert balances["balances"]["KES"] == "0.00"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Idempotency (SPEC.md §6)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestIdempotency:
    def test_idempotent_replay(self, engine, customer_with_balance):
        quote = engine.generate_quote(
            customer_with_balance, "USD", "EUR", Decimal("50")
        )
        first = engine.execute_quote(quote.id, idempotency_key="key-1")
        second = engine.execute_quote(quote.id, idempotency_key="key-1")
        assert first["transaction_id"] == second["transaction_id"]

    def test_different_key_same_quote_fails(
        self, engine, customer_with_balance
    ):
        quote = engine.generate_quote(
            customer_with_balance, "USD", "EUR", Decimal("50")
        )
        engine.execute_quote(quote.id, idempotency_key="key-a")
        with pytest.raises(ValueError, match="already executed"):
            engine.execute_quote(quote.id, idempotency_key="key-b")

    def test_no_key_second_attempt_fails(
        self, engine, customer_with_balance
    ):
        quote = engine.generate_quote(
            customer_with_balance, "USD", "EUR", Decimal("50")
        )
        engine.execute_quote(quote.id)
        with pytest.raises(ValueError, match="already executed"):
            engine.execute_quote(quote.id)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Rate Resolution (SPEC.md §3)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestRateResolution:
    def test_direct_pair(self, engine):
        rate = engine.effective_rate("USD", "KES")
        assert rate > 0

    def test_inverse_pair_preserves_spread(self, engine):
        """KES→USD must equal 1 / USD/KES buy (not mid).
        Using mid would erase the spread (planted_bugs Bug #6).
        """
        usd_kes = engine.rates.get("USD/KES")
        expected = Decimal("1") / usd_kes["buy"]
        actual = engine.effective_rate("KES", "USD")
        assert actual == expected

    def test_cross_pair_via_usd(self, engine):
        """KES→NGN should route through USD."""
        rate = engine.effective_rate("KES", "NGN")
        assert rate > 0

    def test_no_rate_raises(self):
        provider = MagicMock(spec=RateProvider)
        provider.get.return_value = None
        provider.is_critically_stale.return_value = False
        eng = FXEngine(provider)
        with pytest.raises(ValueError, match="no rate"):
            eng.effective_rate("XYZ", "ABC")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Rate Staleness (SPEC.md §9)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestRateStaleness:
    def test_critically_stale_rejects_quotes(
        self, engine, customer_with_balance
    ):
        """If rates are > 15 min old, new quotes must be rejected
        with a clear error (SPEC.md §9).
        """
        engine.rates._last_updated = datetime.now(timezone.utc) - timedelta(
            minutes=20
        )
        with pytest.raises(ValueError, match="critically stale"):
            engine.generate_quote(
                customer_with_balance, "USD", "KES", Decimal("100")
            )

    def test_fresh_rates_accepted(self, engine, customer_with_balance):
        """Rates within threshold should produce quotes normally."""
        quote = engine.generate_quote(
            customer_with_balance, "USD", "KES", Decimal("100")
        )
        assert quote.final_amount > 0
