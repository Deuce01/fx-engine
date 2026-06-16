# SPEC.md — FX Engine Technical Specification

> Written **before** implementation. This document defines the invariants, contracts, and
> failure semantics the codebase must satisfy. Any behavior not described here is explicitly
> out of scope.

---

## 1. Supported Currencies & Pairs

**Currencies:** USD, EUR, KES, NGN (4 currencies, 12 directed pairs).

| Base \ Quote | USD | EUR | KES | NGN |
|---|---|---|---|---|
| **USD** | — | ✓ | ✓ | ✓ |
| **EUR** | ✓ | — | ✓ | ✓ |
| **KES** | ✓ | ✓ | — | ✓ |
| **NGN** | ✓ | ✓ | ✓ | — |

**Minor units:** All four currencies use **2 decimal places** for final output.

---

## 2. Precision & Rounding

| Rule | Detail |
|---|---|
| **Data type** | `decimal.Decimal` exclusively. `float` is **forbidden** for any amount, rate, or balance calculation. |
| **Internal precision** | Full `Decimal` precision is preserved throughout intermediate calculations. |
| **Quantization** | Applied **exactly once**, at the final step, to 2 decimal places (`0.01`). |
| **Rounding mode** | `ROUND_HALF_UP` (banker-unfriendly, but deterministic and intuitive for customers). |
| **Storage** | All monetary values stored as `TEXT` in the database to avoid silent numeric coercion. |

**Invariant:** For any quote, `final_amount == (amount × rate).quantize(Decimal("0.01"), ROUND_HALF_UP)`. This must hold under property-based testing with arbitrary positive amounts and all valid pairs.

---

## 3. FX Routing & Spread Model

### Spread

A fixed **50 basis points (0.5%)** is applied symmetrically around the mid-rate:

```
buy  = mid × (1 - 0.005)    # bank buys base currency (customer sells)
sell = mid × (1 + 0.005)     # bank sells base currency (customer buys)
```

The customer always transacts at the **sell rate** (the rate unfavorable to them).

### Rate Resolution Order

For a requested pair `FROM/TO`:

1. **Direct pair** — If `FROM/TO` exists in the rate table, use its `sell` rate.

2. **Inverse pair** — If only `TO/FROM` exists, compute:
   ```
   effective_rate = 1 / Rate(TO/FROM)["buy"]
   ```
   We invert the `buy` rate (not mid) because inverting the counterparty's buy gives the
   customer the correct unfavorable rate. Reconstructing mid would **erase the spread**.

3. **Cross pair via USD** — If neither direct nor inverse exists, route through USD:
   ```
   leg1 = effective_rate(FROM → USD)   # resolved via rules 1-2 above
   leg2 = effective_rate(USD → TO)     # resolved via rules 1-2 above
   effective_rate = leg1 × leg2
   ```
   Spreads **compound naturally** through multiplication (each leg applies its own spread).
   If USD routing fails, attempt routing through EUR using the same logic.

**Invariant:** Every one of the 12 directed pairs must resolve to a rate. If resolution fails,
the engine returns an error — it never silently falls back to a zero or default rate.

---

## 4. Quote Lifecycle

```
┌──────────┐    generate     ┌───────────┐    execute     ┌──────────┐
│ (no quote)│ ──────────────▶ │   OPEN    │ ──────────────▶ │ EXECUTED │
└──────────┘                 └───────────┘                 └──────────┘
                                   │          60s elapsed
                                   └──────────────────────▶  EXPIRED (implicit)
```

| Property | Value |
|---|---|
| **TTL** | 60 seconds from `created_at`. |
| **Rate lock** | The rate is **frozen at quote creation time**. Execution uses the stored rate, never a fresh one. |
| **Immutability** | A quote is never modified after creation, except for the `executed` flag and `executed_at` timestamp. |
| **One-shot** | A quote can be executed **exactly once**. Subsequent attempts return an error (unless covered by idempotency — see §6). |

---

## 5. Execution — Atomicity

Executing a quote performs **three writes** in a **single ACID transaction**:

1. **Mark quote as executed** — `UPDATE quotes SET executed = 1, executed_at = ? WHERE id = ? AND executed = 0` (atomic check-and-set).
2. **Debit source balance** — Decrease `customer.balances[from_currency]` by `amount`.
3. **Credit destination balance** — Increase `customer.balances[to_currency]` by `final_amount`.
4. **Record transaction** — Insert into `transactions` table with all details.
5. **Store idempotency key → response** (if key provided).

If **any** step fails, the entire transaction rolls back. Specifically:

| Failure | Behavior |
|---|---|
| Quote not found | `400` — "quote not found" |
| Quote expired (`expires_at < now`) | `400` — "quote expired" |
| Quote already executed (`executed = 1`) | `400` — "quote already executed" |
| Insufficient source balance | `400` — "insufficient balance" (both balances unchanged) |
| Database error | `500` — full rollback, no partial state |

**Invariant:** The sum of all customer balances in a given currency, across all customers,
changes by exactly zero for every executed transaction (money is neither created nor destroyed; it transfers between the customer's own currency accounts).

---

## 6. Idempotency

| Rule | Detail |
|---|---|
| **Key source** | `Idempotency-Key` HTTP header (client-provided, opaque string). |
| **Scope** | Per execution request. The key is globally unique, not scoped to a quote. |
| **Check timing** | The idempotency lookup happens **inside** the execution transaction, before any writes, to eliminate TOCTOU races between concurrent retries. |
| **Cache behavior** | If the key exists, return the stored response immediately with the original status code — no re-execution occurs. |
| **No key provided** | Execution proceeds without idempotency protection. A second attempt on the same quote returns "already executed." |
| **Storage** | `idempotency(key TEXT PRIMARY KEY, response TEXT, created_at TEXT)` — persisted in the same database. |

**Invariant:** Two concurrent requests with the same idempotency key produce **identical** responses, and exactly one execution occurs.

---

## 7. Concurrency Model

The system must safely handle **N simultaneous** execution requests for the same `quote_id`.

| Concern | Mechanism |
|---|---|
| **Race prevention** | Atomic conditional update: `UPDATE quotes SET executed = 1 WHERE id = ? AND executed = 0`. Only one concurrent caller gets `rowcount = 1`; all others see `rowcount = 0` and return an error. |
| **Balance integrity** | Balance reads and writes happen within the same serialized transaction. |
| **What we do NOT use** | `threading.Lock` or any in-process mutex. These fail in multi-worker (gunicorn/uvicorn) and multi-instance deployments. |
| **Database choice** | SQLite in WAL mode with `IMMEDIATE` transactions for serialized writes, or PostgreSQL with `SELECT ... FOR UPDATE`. |

**Invariant:** Given N concurrent `execute` calls for the same quote, exactly **1** succeeds and **N−1** fail. No double-debit, no double-credit.

---

## 8. Customer Balances

| Operation | Endpoint | Behavior |
|---|---|---|
| **Create customer** | `POST /customers` | Creates a customer with zero balances in all 4 currencies. Returns `customer_id`. |
| **View balances** | `GET /customers/<id>/balances` | Returns current balance per currency. |
| **Credit balance** | `POST /customers/<id>/balances/credit` | Manually add funds to a specific currency. Test fixture — no real payment integration. |
| **Debit/credit via execute** | `POST /quotes/<id>/execute` | Debit `from_currency`, credit `to_currency` — happens atomically (see §5). |

**Invariant:** No balance may go negative. An execution that would cause a negative source balance is rejected before any writes occur.

**Schema:**
```sql
CREATE TABLE balances (
    customer_id TEXT NOT NULL,
    currency    TEXT NOT NULL CHECK(currency IN ('USD','EUR','KES','NGN')),
    amount      TEXT NOT NULL DEFAULT '0',
    PRIMARY KEY (customer_id, currency)
);
```

---

## 9. Rate Provider & Failure Policy

| Concern | Policy |
|---|---|
| **Source** | Mock seed rates (development) or free-tier API (e.g., exchangeratesapi.io). |
| **Cache** | Rates cached in-memory with a `last_updated` timestamp. |
| **Refresh** | On-demand via `POST /rates/refresh`, or on a background schedule. |
| **Staleness ≤ 5 min** | Serve cached rates normally. |
| **Staleness 5–15 min** | Serve cached rates with a `X-Rates-Stale: true` response header and a warning log. |
| **Staleness > 15 min** | **Reject** all new quote requests with `503 Service Unavailable`. Execution of existing (unexpired) quotes still proceeds since their rate is already locked. |
| **Upstream timeout** | 5-second timeout on upstream HTTP calls. On failure, increment a failure counter. After 3 consecutive failures, stop retrying for 60 seconds (circuit breaker). |

---

## 10. Observability

| Feature | Implementation |
|---|---|
| **Correlation ID** | Every request gets a `correlation_id` (from `X-Request-Id` header, or auto-generated UUID). Included in all log entries and all responses. |
| **Structured logging** | JSON-formatted logs: `{"timestamp", "level", "correlation_id", "event", "quote_id", "customer_id", ...}`. |
| **Health check** | `GET /healthz` — returns `200` if the database is reachable and rates are not critically stale (< 15 min). Returns `503` otherwise. |
| **Metrics endpoint** | `GET /metrics` — returns JSON counters: `quotes_created`, `quotes_executed`, `quotes_expired`, `execution_errors`, `rate_refresh_failures`, `avg_execution_time_ms`. |
| **Event trail** | Every quote creation and execution is logged with the correlation ID, enabling end-to-end tracing from quote → execute. |

---

## 11. API Surface Summary

| Method | Path | Purpose | Success | Key Errors |
|---|---|---|---|---|
| `POST` | `/customers` | Create customer | `201` | `400` invalid input |
| `GET` | `/customers/<id>/balances` | View balances | `200` | `404` customer not found |
| `POST` | `/customers/<id>/balances/credit` | Credit test funds | `200` | `400` invalid amount/currency, `404` not found |
| `POST` | `/quotes` | Generate FX quote | `201` | `400` invalid pair/amount, `503` rates stale |
| `POST` | `/quotes/<id>/execute` | Execute quote | `200` | `400` expired/executed/insufficient, `404` not found |
| `POST` | `/rates/refresh` | Refresh exchange rates | `200` | `503` upstream failure |
| `GET` | `/rates` | View current rates | `200` | — |
| `GET` | `/healthz` | Health check | `200` | `503` unhealthy |
| `GET` | `/metrics` | Operational metrics | `200` | — |

---

## 12. Out of Scope

- Authentication and authorization (all endpoints are unauthenticated).
- Multi-tenancy or user-tier-based spread adjustments (fixed 50 bps for all).
- Historical reporting, audit log queries, or admin dashboards.
- Real payment rails or external ledger integration.
- Rate-limiting or throttling.
- Deployment, containerization, or infrastructure (documented in README under "what I'd do next").
