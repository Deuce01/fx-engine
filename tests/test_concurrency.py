"""Concurrency tests: parallel execution of the same quote.

Proves that exactly 1 of N concurrent execution attempts succeeds,
with no double-debits or double-credits (SPEC.md §7).
"""
from __future__ import annotations

import threading
from decimal import Decimal
from typing import List

import pytest


class TestConcurrentExecution:
    def test_exactly_one_succeeds(self, engine, customer_with_balance):
        """Fire 10 parallel execute calls for the same quote.

        Assert: exactly 1 succeeds, 9 fail with 'already executed'.
        """
        quote = engine.generate_quote(
            customer_with_balance, "USD", "KES", Decimal("100")
        )

        results: List[dict | Exception] = [None] * 10

        def execute(index: int):
            try:
                results[index] = engine.execute_quote(quote.id)
            except Exception as exc:
                results[index] = exc

        threads = [
            threading.Thread(target=execute, args=(i,)) for i in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        successes = [r for r in results if isinstance(r, dict)]
        failures = [r for r in results if isinstance(r, Exception)]

        assert len(successes) == 1, (
            f"Expected 1 success, got {len(successes)}"
        )
        assert len(failures) == 9
        for f in failures:
            assert "already executed" in str(f)

    def test_no_double_debit(self, engine, customer_with_balance):
        """After N concurrent attempts, source is debited exactly once."""
        before = engine.get_balances(customer_with_balance)
        usd_before = Decimal(before["balances"]["USD"])

        quote = engine.generate_quote(
            customer_with_balance, "USD", "EUR", Decimal("500")
        )

        results: list = []

        def safe_execute(q_id: str):
            try:
                results.append(engine.execute_quote(q_id))
            except ValueError:
                pass

        threads = [
            threading.Thread(target=safe_execute, args=(quote.id,))
            for _ in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        after = engine.get_balances(customer_with_balance)
        usd_after = Decimal(after["balances"]["USD"])

        # Exactly $500 debited, not $2 500
        assert usd_after == usd_before - Decimal("500")
        assert len(results) == 1

    def test_concurrent_idempotent_execution(
        self, engine, customer_with_balance
    ):
        """Multiple concurrent requests with the same idempotency key
        must all return the same transaction_id.
        """
        quote = engine.generate_quote(
            customer_with_balance, "USD", "KES", Decimal("200")
        )

        results: List[dict | Exception] = [None] * 5

        def execute(index: int):
            try:
                results[index] = engine.execute_quote(
                    quote.id, idempotency_key="same-key"
                )
            except Exception as exc:
                results[index] = exc

        threads = [
            threading.Thread(target=execute, args=(i,)) for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        successes = [r for r in results if isinstance(r, dict)]
        # All successful responses share the same transaction_id
        tx_ids = {r["transaction_id"] for r in successes}
        assert len(tx_ids) == 1, f"Expected 1 unique tx_id, got {tx_ids}"
