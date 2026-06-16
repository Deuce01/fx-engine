"""FX Engine core: quote generation, atomic execution, rate resolution,
customer balances, and rate-source failure handling.

All financial calculations use ``decimal.Decimal`` exclusively.
``float`` is **forbidden**.  Rounding (``ROUND_HALF_UP`` to 2 dp)
is applied exactly once at the final output step.

Concurrency safety is enforced at the database level via SQLite
``BEGIN IMMEDIATE`` transactions — never via ``threading.Lock``
(SPEC.md §7).
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, Optional

from .database import get_connection, get_read_connection
from .models import Quote, SUPPORTED_CURRENCIES

logger = logging.getLogger(__name__)

# ─── Constants (from SPEC.md) ────────────────────────────────────

QUOTE_TTL_SECONDS = 60
QUANTUM = Decimal("0.01")          # 2 decimal-place quantizer
SPREAD_BPS = Decimal("0.005")      # 50 basis points each side

# Staleness thresholds (seconds) — SPEC.md §9
RATE_WARN_THRESHOLD = 300          # 5 minutes  → serve with warning
RATE_REJECT_THRESHOLD = 900        # 15 minutes → reject new quotes

# Circuit-breaker settings — SPEC.md §9
MAX_CONSECUTIVE_FAILURES = 3
CIRCUIT_BREAKER_COOLDOWN = 60      # seconds

# Seed mid-rates (production would hit exchangeratesapi.io)
_SEED_MID_RATES: Dict[str, Decimal] = {
    "USD/EUR": Decimal("0.92"),
    "USD/KES": Decimal("129.50"),
    "USD/NGN": Decimal("1480.00"),
    "EUR/KES": Decimal("140.75"),
    "EUR/NGN": Decimal("1608.50"),
}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Rate Provider
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class RateProvider:
    """Manages FX rates with buy/sell spreads, staleness tracking,
    and a circuit-breaker for upstream failures.
    """

    def __init__(self) -> None:
        self._rates: Dict[str, Dict[str, Decimal]] = {}
        self._last_updated: datetime = datetime.now(timezone.utc)
        self._consecutive_failures: int = 0
        self._circuit_open_until: Optional[float] = None
        self._refresh_from_seed()

    # ── internal helpers ──────────────────────────────────────────

    @staticmethod
    def _apply_spread(mid: Decimal) -> Dict[str, Decimal]:
        """Apply symmetric 50-bps spread around a mid-rate."""
        return {
            "buy":  mid * (Decimal("1") - SPREAD_BPS),
            "sell": mid * (Decimal("1") + SPREAD_BPS),
        }

    def _refresh_from_seed(self) -> None:
        """Load rates from seed data (simulates upstream API call)."""
        self._rates = {
            pair: self._apply_spread(mid)
            for pair, mid in _SEED_MID_RATES.items()
        }
        self._last_updated = datetime.now(timezone.utc)
        self._consecutive_failures = 0
        self._circuit_open_until = None

    # ── public API ────────────────────────────────────────────────

    def refresh(self) -> Dict[str, Any]:
        """Attempt to refresh rates from upstream.

        Implements a circuit-breaker: after ``MAX_CONSECUTIVE_FAILURES``
        consecutive errors, stops retrying for ``CIRCUIT_BREAKER_COOLDOWN``
        seconds (SPEC.md §9).
        """
        if (
            self._circuit_open_until is not None
            and time.monotonic() < self._circuit_open_until
        ):
            remaining = int(self._circuit_open_until - time.monotonic())
            logger.warning("circuit breaker open, retry in %ds", remaining)
            return {
                "status": "circuit_open",
                "retry_after_seconds": remaining,
                "last_updated": self._last_updated.isoformat(),
            }

        try:
            # In production, this would call exchangeratesapi.io
            self._refresh_from_seed()
            logger.info("rates refreshed successfully")
            return {
                "status": "ok",
                "updated_at": self._last_updated.isoformat(),
            }
        except Exception as exc:
            self._consecutive_failures += 1
            logger.error(
                "rate refresh failed (attempt %d): %s",
                self._consecutive_failures,
                exc,
            )
            if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                self._circuit_open_until = (
                    time.monotonic() + CIRCUIT_BREAKER_COOLDOWN
                )
                logger.warning(
                    "circuit breaker OPEN for %ds", CIRCUIT_BREAKER_COOLDOWN
                )
            return {
                "status": "error",
                "error": str(exc),
                "consecutive_failures": self._consecutive_failures,
                "last_updated": self._last_updated.isoformat(),
            }

    def get(self, pair: str) -> Optional[Dict[str, Decimal]]:
        """Return buy/sell rates for *pair*, or ``None`` if unavailable."""
        return self._rates.get(pair)

    def snapshot(self) -> Dict[str, Dict[str, str]]:
        """All rates serialized to strings (for JSON responses)."""
        return {
            pair: {"buy": str(v["buy"]), "sell": str(v["sell"])}
            for pair, v in self._rates.items()
        }

    def staleness_seconds(self) -> float:
        """Seconds elapsed since the last successful rate update."""
        return (datetime.now(timezone.utc) - self._last_updated).total_seconds()

    def is_stale(self) -> bool:
        """True when rates exceed the warning threshold (5 min)."""
        return self.staleness_seconds() > RATE_WARN_THRESHOLD

    def is_critically_stale(self) -> bool:
        """True when rates exceed the rejection threshold (15 min)."""
        return self.staleness_seconds() > RATE_REJECT_THRESHOLD

    def last_updated_iso(self) -> str:
        return self._last_updated.isoformat()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  FX Engine
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class FXEngine:
    """Core FX engine: quote generation, atomic execution, and
    customer-balance management.

    All concurrency control is at the database level via
    ``BEGIN IMMEDIATE`` (SPEC.md §7).
    """

    def __init__(
        self,
        rate_provider: RateProvider,
        db_path=None,
    ) -> None:
        self.rates = rate_provider
        self._db_path = db_path
        self.metrics: Dict[str, int] = {
            "quotes_created": 0,
            "quotes_executed": 0,
            "quotes_expired": 0,
            "execution_errors": 0,
            "rate_refresh_failures": 0,
        }

    # ── Rate Resolution (SPEC.md §3) ─────────────────────────────

    def effective_rate(self, from_ccy: str, to_ccy: str) -> Decimal:
        """Resolve the effective sell-side rate for a currency pair.

        Resolution order (SPEC.md §3):
          1. Direct pair  → use ``sell`` rate.
          2. Inverse pair → ``1 / buy`` (preserves spread direction).
          3. Cross pair   → route through USD, then EUR as fallback.

        Raises ``ValueError`` when no rate can be resolved.
        """
        if from_ccy == to_ccy:
            raise ValueError("from and to currencies must differ")

        # 1. Direct pair (Base -> Quote). Bank buys Base, pays 'buy' rate.
        direct = self.rates.get(f"{from_ccy}/{to_ccy}")
        if direct is not None:
            return direct["buy"]

        # 2. Inverse pair (Quote -> Base). Bank sells Base, charges 'sell' rate.
        # Customer gets 1 / sell.
        inverse = self.rates.get(f"{to_ccy}/{from_ccy}")
        if inverse is not None:
            return Decimal("1") / inverse["sell"]

        # 3. Cross pair via a base currency (USD first, then EUR)
        for base in ("USD", "EUR"):
            if from_ccy == base or to_ccy == base:
                continue
            try:
                leg1 = self._resolve_single_leg(from_ccy, base)
                leg2 = self._resolve_single_leg(base, to_ccy)
                return leg1 * leg2
            except ValueError:
                continue

        raise ValueError(f"no rate available for {from_ccy}/{to_ccy}")

    def _resolve_single_leg(self, from_ccy: str, to_ccy: str) -> Decimal:
        """Resolve a single leg (direct or inverse only — no cross)."""
        direct = self.rates.get(f"{from_ccy}/{to_ccy}")
        if direct is not None:
            return direct["buy"]

        inverse = self.rates.get(f"{to_ccy}/{from_ccy}")
        if inverse is not None:
            return Decimal("1") / inverse["sell"]

        raise ValueError(f"no direct/inverse rate for {from_ccy}/{to_ccy}")

    # ── Customer Management ───────────────────────────────────────

    def create_customer(self, name: str) -> dict:
        """Create a customer with zero balances in all supported currencies."""
        customer_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)

        with get_connection(self._db_path) as conn:
            conn.execute(
                "INSERT INTO customers (id, name, created_at) VALUES (?, ?, ?)",
                (customer_id, name, now.isoformat()),
            )
            for currency in sorted(SUPPORTED_CURRENCIES):
                conn.execute(
                    "INSERT INTO balances (customer_id, currency, amount) "
                    "VALUES (?, ?, ?)",
                    (customer_id, currency, "0.00"),
                )

        logger.info("customer created", extra={"customer_id": customer_id})
        return {
            "customer_id": customer_id,
            "name": name,
            "created_at": now.isoformat(),
        }

    def get_balances(self, customer_id: str) -> dict:
        """Return current balances for every currency held by *customer_id*."""
        with get_read_connection(self._db_path) as conn:
            cust = conn.execute(
                "SELECT id FROM customers WHERE id = ?", (customer_id,)
            ).fetchone()
            if cust is None:
                raise ValueError("customer not found")

            rows = conn.execute(
                "SELECT currency, amount FROM balances "
                "WHERE customer_id = ? ORDER BY currency",
                (customer_id,),
            ).fetchall()

        return {
            "customer_id": customer_id,
            "balances": {row["currency"]: row["amount"] for row in rows},
        }

    def credit_balance(
        self, customer_id: str, currency: str, amount: Decimal
    ) -> dict:
        """Manually add funds to a customer balance (test fixture).

        Raises ``ValueError`` for non-positive amounts, unknown customers,
        or unsupported currencies.
        """
        if amount <= 0:
            raise ValueError("amount must be positive")
        currency = currency.upper()
        if currency not in SUPPORTED_CURRENCIES:
            raise ValueError(f"unsupported currency: {currency}")

        credit = amount.quantize(QUANTUM, rounding=ROUND_HALF_UP)

        with get_connection(self._db_path) as conn:
            row = conn.execute(
                "SELECT amount FROM balances "
                "WHERE customer_id = ? AND currency = ?",
                (customer_id, currency),
            ).fetchone()
            if row is None:
                raise ValueError("customer or currency not found")

            current = Decimal(row["amount"])
            new_balance = (current + credit).quantize(
                QUANTUM, rounding=ROUND_HALF_UP
            )
            conn.execute(
                "UPDATE balances SET amount = ? "
                "WHERE customer_id = ? AND currency = ?",
                (str(new_balance), customer_id, currency),
            )

        logger.info(
            "balance credited",
            extra={
                "customer_id": customer_id,
                "currency": currency,
                "amount": str(credit),
            },
        )
        return {
            "customer_id": customer_id,
            "currency": currency,
            "credited": str(credit),
            "new_balance": str(new_balance),
        }

    # ── Quote Generation (SPEC.md §4) ────────────────────────────

    def generate_quote(
        self,
        customer_id: str,
        from_ccy: str,
        to_ccy: str,
        amount: Decimal,
    ) -> Quote:
        """Generate an FX quote with a **locked** rate.

        The rate is frozen at creation time; execution will use
        this stored rate — never a fresh one (SPEC.md §4).

        Raises ``ValueError`` if:
          - amount ≤ 0
          - from == to currency
          - rates are critically stale (> 15 min)
          - customer does not exist
        """
        if amount <= 0:
            raise ValueError("amount must be positive")
        if from_ccy == to_ccy:
            raise ValueError("from and to currencies must differ")

        # Reject if rates too old (SPEC.md §9)
        if self.rates.is_critically_stale():
            self.metrics["rate_refresh_failures"] += 1
            raise ValueError(
                "rates are critically stale (>15 min); cannot generate quotes"
            )

        rate = self.effective_rate(from_ccy, to_ccy)
        final_amount = (amount * rate).quantize(QUANTUM, rounding=ROUND_HALF_UP)

        now = datetime.now(timezone.utc)
        quote_id = str(uuid.uuid4())

        quote = Quote(
            id=quote_id,
            customer_id=customer_id,
            from_currency=from_ccy,
            to_currency=to_ccy,
            amount=amount,
            rate=rate,
            final_amount=final_amount,
            created_at=now,
            expires_at=now + timedelta(seconds=QUOTE_TTL_SECONDS),
        )

        with get_connection(self._db_path) as conn:
            # Verify customer exists
            cust = conn.execute(
                "SELECT id FROM customers WHERE id = ?", (customer_id,)
            ).fetchone()
            if cust is None:
                raise ValueError("customer not found")

            conn.execute(
                """INSERT INTO quotes
                   (id, customer_id, from_currency, to_currency, amount,
                    rate, final_amount, created_at, expires_at, executed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
                (
                    quote.id,
                    customer_id,
                    from_ccy,
                    to_ccy,
                    str(amount),
                    str(rate),
                    str(final_amount),
                    now.isoformat(),
                    quote.expires_at.isoformat(),
                ),
            )

        self.metrics["quotes_created"] += 1
        logger.info(
            "quote created",
            extra={
                "quote_id": quote_id,
                "customer_id": customer_id,
                "pair": f"{from_ccy}/{to_ccy}",
                "amount": str(amount),
                "rate": str(rate),
                "final_amount": str(final_amount),
            },
        )
        return quote

    # ── Quote Execution (SPEC.md §5) ─────────────────────────────

    def execute_quote(
        self,
        quote_id: str,
        idempotency_key: Optional[str] = None,
    ) -> dict:
        """Execute a quote atomically: debit source, credit destination.

        **All** of the following happen inside a single ``BEGIN IMMEDIATE``
        transaction (SPEC.md §5):

          1. Idempotency check  (if key provided)
          2. Load + validate quote (not expired, not yet executed)
          3. Atomic check-and-set: ``UPDATE … WHERE executed = 0``
          4. Debit source balance (fail if insufficient)
          5. Credit destination balance
          6. Record transaction
          7. Store idempotency key → response

        If **any** step fails, the entire transaction rolls back.
        """
        with get_connection(self._db_path) as conn:
            # ── Step 1: idempotency (INSIDE transaction — SPEC §6) ──
            if idempotency_key:
                cached = conn.execute(
                    "SELECT response FROM idempotency WHERE key = ?",
                    (idempotency_key,),
                ).fetchone()
                if cached:
                    logger.info(
                        "idempotent replay",
                        extra={
                            "idempotency_key": idempotency_key,
                            "quote_id": quote_id,
                        },
                    )
                    return json.loads(cached["response"])

            # ── Step 2: load and validate quote ─────────────────────
            row = conn.execute(
                "SELECT * FROM quotes WHERE id = ?", (quote_id,)
            ).fetchone()
            if row is None:
                raise ValueError("quote not found")

            now = datetime.now(timezone.utc)
            expires_at = datetime.fromisoformat(row["expires_at"])
            if expires_at < now:
                self.metrics["quotes_expired"] += 1
                raise ValueError("quote expired")

            # ── Step 3: atomic check-and-set (SPEC §7) ──────────────
            result = conn.execute(
                "UPDATE quotes SET executed = 1, executed_at = ? "
                "WHERE id = ? AND executed = 0",
                (now.isoformat(), quote_id),
            )
            if result.rowcount == 0:
                raise ValueError("quote already executed")

            # Use the LOCKED rate from quote creation (SPEC §4)
            amount = Decimal(row["amount"])
            rate = Decimal(row["rate"])
            final_amount = Decimal(row["final_amount"])
            customer_id = row["customer_id"]
            from_ccy = row["from_currency"]
            to_ccy = row["to_currency"]

            # ── Step 4: debit source balance ─────────────────────────
            src_row = conn.execute(
                "SELECT amount FROM balances "
                "WHERE customer_id = ? AND currency = ?",
                (customer_id, from_ccy),
            ).fetchone()
            if src_row is None:
                raise ValueError("source balance not found")

            source_balance = Decimal(src_row["amount"])
            if source_balance < amount:
                self.metrics["execution_errors"] += 1
                raise ValueError(
                    f"insufficient {from_ccy} balance: "
                    f"have {source_balance}, need {amount}"
                )

            new_source = (source_balance - amount).quantize(
                QUANTUM, rounding=ROUND_HALF_UP
            )
            conn.execute(
                "UPDATE balances SET amount = ? "
                "WHERE customer_id = ? AND currency = ?",
                (str(new_source), customer_id, from_ccy),
            )

            # ── Step 5: credit destination balance ───────────────────
            dst_row = conn.execute(
                "SELECT amount FROM balances "
                "WHERE customer_id = ? AND currency = ?",
                (customer_id, to_ccy),
            ).fetchone()
            dest_balance = Decimal(dst_row["amount"])
            new_dest = (dest_balance + final_amount).quantize(
                QUANTUM, rounding=ROUND_HALF_UP
            )
            conn.execute(
                "UPDATE balances SET amount = ? "
                "WHERE customer_id = ? AND currency = ?",
                (str(new_dest), customer_id, to_ccy),
            )

            # ── Step 6: record transaction ───────────────────────────
            tx_id = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO transactions
                   (id, quote_id, customer_id, from_currency, to_currency,
                    amount, final_amount, rate, executed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    tx_id,
                    quote_id,
                    customer_id,
                    from_ccy,
                    to_ccy,
                    str(amount),
                    str(final_amount),
                    str(rate),
                    now.isoformat(),
                ),
            )

            response = {
                "transaction_id": tx_id,
                "quote_id": quote_id,
                "customer_id": customer_id,
                "from_currency": from_ccy,
                "to_currency": to_ccy,
                "amount": str(amount),
                "final_amount": str(final_amount),
                "rate": str(rate),
                "executed_at": now.isoformat(),
            }

            # ── Step 7: store idempotency key ────────────────────────
            if idempotency_key:
                conn.execute(
                    "INSERT INTO idempotency (key, response, created_at) "
                    "VALUES (?, ?, ?)",
                    (idempotency_key, json.dumps(response), now.isoformat()),
                )

        self.metrics["quotes_executed"] += 1
        logger.info(
            "quote executed",
            extra={
                "transaction_id": tx_id,
                "quote_id": quote_id,
                "customer_id": customer_id,
                "pair": f"{from_ccy}/{to_ccy}",
                "amount": str(amount),
                "final_amount": str(final_amount),
            },
        )
        return response
