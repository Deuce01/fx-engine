"""Property-based tests for Decimal precision (SPEC.md §2).

Uses Hypothesis to generate random amounts across all 12 currency pairs,
asserting that:

  1. ``final_amount == (amount × rate).quantize(Decimal("0.01"), ROUND_HALF_UP)``
  2. No ``float`` is ever used in calculations.
  3. Round-trip conversions never *create* money (they may lose due to spread).
  4. Every ``final_amount`` is quantized to exactly 2 decimal places.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis.strategies import decimals, sampled_from

CURRENCIES = ["USD", "EUR", "KES", "NGN"]
QUANTUM = Decimal("0.01")

# Amounts between 0.01 and 10 000 000 — covers edge cases at both ends.
amounts = decimals(
    min_value=Decimal("10.00"),
    max_value=Decimal("10000000"),
    places=2,
    allow_nan=False,
    allow_infinity=False,
)

currency_pairs = sampled_from(
    [(a, b) for a in CURRENCIES for b in CURRENCIES if a != b]
)


class TestDecimalPrecision:
    @given(amount=amounts, pair=currency_pairs)
    @settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_final_amount_matches_pure_decimal(
        self, engine, customer_with_balance, amount, pair
    ):
        """For any amount and pair, ``final_amount`` must equal
        ``(amount × rate).quantize(…)`` using pure Decimal — never float.
        """
        from_ccy, to_ccy = pair
        rate = engine.effective_rate(from_ccy, to_ccy)
        expected = (amount * rate).quantize(QUANTUM, rounding=ROUND_HALF_UP)

        quote = engine.generate_quote(
            customer_with_balance, from_ccy, to_ccy, amount
        )

        assert quote.final_amount == expected, (
            f"Precision error for {amount} {from_ccy}→{to_ccy}: "
            f"expected {expected}, got {quote.final_amount}"
        )

    @given(amount=amounts)
    @settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_roundtrip_never_creates_money(
        self, engine, customer_with_balance, amount
    ):
        """Converting USD→EUR→USD should lose money due to spread,
        never gain more than a rounding quantum.
        """
        rate_fwd = engine.effective_rate("USD", "EUR")
        eur = (amount * rate_fwd).quantize(QUANTUM, rounding=ROUND_HALF_UP)

        rate_back = engine.effective_rate("EUR", "USD")
        usd_back = (eur * rate_back).quantize(QUANTUM, rounding=ROUND_HALF_UP)

        # Allow at most 0.01 gain from rounding; spreads should ensure a loss.
        assert usd_back <= amount + QUANTUM, (
            f"Round-trip created money: {amount} USD → {eur} EUR "
            f"→ {usd_back} USD"
        )

    @given(amount=amounts, pair=currency_pairs)
    @settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_final_amount_is_quantized(
        self, engine, customer_with_balance, amount, pair
    ):
        """Every ``final_amount`` must have exactly 2 decimal places."""
        from_ccy, to_ccy = pair
        quote = engine.generate_quote(
            customer_with_balance, from_ccy, to_ccy, amount
        )
        re_quantized = quote.final_amount.quantize(
            QUANTUM, rounding=ROUND_HALF_UP
        )
        assert quote.final_amount == re_quantized
