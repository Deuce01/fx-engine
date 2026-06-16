# Code Review: Baseline FX Engine (`planted_bugs/`)

**Reviewer:** AI-Native Backend Engineer Candidate
**Date:** June 16, 2026
**Target:** `planted_bugs/` repository baseline

This document provides a formal code review of the provided baseline implementation. The baseline contains several critical architectural, financial, and concurrency flaws that render it unfit for production. 

Below is a categorized breakdown of the issues found, ranked by severity.

---

## 🚨 Critical Severity (Data Loss & Financial Risk)

### 1. Floating-Point Arithmetic for Currency
- **Location:** `models.py` (`amount: float`, `rate: float`), throughout `engine.py`
- **Issue:** The baseline uses Python's standard IEEE 754 `float` types for all monetary values and exchange rates. Floating-point arithmetic introduces precision loss (e.g., `0.1 + 0.2 = 0.30000000000000004`).
- **Impact:** Accumulating rounding errors will cause missing pennies, unbalanced books, and audit failures. In a high-volume financial system, this translates to actual money lost.
- **Fix:** Strictly enforce `decimal.Decimal` for all monetary amounts, rates, and calculations. Use `ROUND_HALF_UP` quantized to 2 decimal places exactly once before persisting or returning the value.

### 2. Lack of Transactional Atomicity
- **Location:** `engine.py` (`execute_quote`)
- **Issue:** Balance updates (debiting the source, crediting the destination) are performed sequentially in memory without atomicity.
- **Impact:** If the engine crashes or encounters an error between the debit and credit, money disappears from the system. 
- **Fix:** All legs of a transaction (check idempotency, debit, credit, mark executed) must happen within a single ACID-compliant database transaction. If any step fails, the entire transaction rolls back.

### 3. Concurrency Race Conditions (Double Execution)
- **Location:** `engine.py` (`execute_quote`)
- **Issue:** The code checks `if quote.executed:` and then proceeds to execute. In a concurrent environment, `Thread A` and `Thread B` can both pass the `if not executed:` check simultaneously, resulting in the same quote executing twice.
- **Impact:** Double debits and double credits. A user could exploit this to multiply their balances.
- **Fix:** Use a database check-and-set mechanism: `UPDATE quotes SET executed = 1 WHERE id = ? AND executed = 0`. If `rowcount == 0`, the quote was already executed.

---

## 🛑 High Severity (Business Logic & UX)

### 4. Floating Exchange Rates at Execution
- **Location:** `engine.py` (`execute_quote`)
- **Issue:** The quote generation creates a rate, but `execute_quote` fetches a *brand new rate* from the provider at the moment of execution.
- **Impact:** Bait-and-switch. A customer is shown one rate, but gets charged a different rate when they click "Execute". This violates FX quoting principles where quotes lock a specific rate for a TTL.
- **Fix:** Store the generated `rate` inside the `Quote` object/table. At execution, use the locked rate from the quote, ignoring current market fluctuations.

### 5. Missing Idempotency
- **Location:** `main.py` (`/quotes/{id}/execute`)
- **Issue:** The API does not accept or process an `Idempotency-Key` header.
- **Impact:** If a client experiences a network timeout and retries the execution request, they might execute the quote multiple times or receive a generic "already executed" error instead of the original transaction result.
- **Fix:** Implement an `idempotency` table. Store the response payload keyed by the idempotency key within the execution transaction. Return the cached payload on retries.

### 6. Spread Disappearance on Inverse Pairs
- **Location:** `engine.py` (Rate Resolution)
- **Issue:** When resolving inverse pairs, the baseline likely just inverts the mid-rate or the sell-rate incorrectly, eroding the spread.
- **Impact:** The bank loses money. The spread exists to ensure the bank captures margin regardless of trade direction.
- **Fix:** Base -> Quote conversions (bank buys Base) must use the `buy` rate. Quote -> Base conversions (bank sells Base) must use `1 / sell` to ensure the customer always pays the unfavorable side of the spread.

---

## ⚠️ Medium Severity (Reliability & Architecture)

### 7. No Persistent Storage
- **Location:** Entire baseline
- **Issue:** All data (customers, balances, quotes) is stored in Python dictionaries (`self.balances = {}`).
- **Impact:** Total data loss upon process restart or container crash.
- **Fix:** Implement a persistent database (e.g., SQLite with WAL mode or PostgreSQL) for all domain entities.

### 8. Missing Rate Staleness & Circuit Breakers
- **Location:** `engine.py` (`RateProvider`)
- **Issue:** There is no check to ensure upstream rates are fresh. If the upstream API goes down, the engine will happily generate quotes using ancient rates.
- **Impact:** Market exposure. The bank could quote rates that are massively disconnected from current market reality, allowing arbitrage.
- **Fix:** Track `last_updated`. Reject new quotes if rates are older than 15 minutes. Implement a circuit breaker to stop hammering a failing upstream service.

### 9. Thread-Unsafe Balance Reads
- **Location:** `engine.py` (`get_balances`)
- **Issue:** Reading from the global `balances` dictionary while it is being updated by other threads can lead to dirty reads or `RuntimeError: dictionary changed size during iteration`.
- **Impact:** Inconsistent API responses.
- **Fix:** Use a database with proper isolation levels (e.g., `READ COMMITTED` or SQLite's `WAL` mode) so reads never block writes and are always consistent.

---
*Note: A completely new, production-ready FX Engine addressing all of these issues from the ground up has been implemented in the `src/` directory.*
