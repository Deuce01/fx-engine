# FX Engine (Umba Take-Home)

A production-grade, AI-native foreign exchange engine built for concurrency, strict financial precision, and idempotency.

## 🚀 Key Features
- **Zero-Float Architecture:** Strict enforcement of `decimal.Decimal` across the entire codebase. Rounding (`ROUND_HALF_UP`) occurs exactly once before output.
- **Database-Level Concurrency:** Built on SQLite in `WAL` mode using manual `BEGIN IMMEDIATE` transactions. Ensures perfect atomicity (no double executions, no race conditions) without relying on fragile application-level `threading.Lock()` limits.
- **Strict Idempotency:** API execution endpoints support an `Idempotency-Key` header, caching transaction responses safely against network retries.
- **Spread Preservation:** Cross-rates and inverse pairs correctly preserve the bank's margin (Bid/Ask logic).
- **Staleness Tracking:** Built-in circuit breakers and TTLs prevent the generation of quotes from ancient rates.

## 📁 Repository Structure
- `src/` — The pristine, brand-new FX engine architecture.
  - `database.py`: ACID transaction and schema definitions.
  - `engine.py`: Core logic for rate resolution, quotes, and execution.
  - `models.py`: Immutable dataclasses and Pydantic schemas.
  - `main.py`: FastAPI application.
- `tests/` — Comprehensive test suite including concurrency and property-based precision testing.
- `planted_bugs/` — The original buggy baseline (ignored by Git, but reviewed in `REVIEW.md`).

## 📚 Deliverables Checklist
- [x] Codebase implemented (`src/`)
- [x] Concurrency and Precision tests (`tests/`)
- [x] `SPEC.md` — The technical specification and system invariants.
- [x] `REVIEW.md` — The code review of the buggy baseline.
- [x] `DECISIONS.md` — Architectural trade-offs and AI interactions.
- [x] `AGENTS.md` — Instructions and constraints given to the AI.

## 🛠️ Setup & Execution

### 1. Install Dependencies
```bash
python -m venv .venv
source .venv/bin/activate  # On Windows use: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Run the Test Suite
The test suite utilizes `pytest` alongside `hypothesis` to prove precision and concurrency safety across thousands of iterations.
```bash
python -m pytest tests/ -v
```

### 3. Run the API Server
```bash
uvicorn src.main:app --reload
```
View the interactive API documentation at: http://127.0.0.1:8000/docs

## ⏱️ Time Budget Breakdown
* **Wall-Clock Time:** ~36 hours (Received the assignment Tuesday evening, submitted Thursday morning).
* **Active Engagement Time:** ~6 hours total.
  * *1 hr:* Reviewing baseline and writing `SPEC.md`.
  * *3 hrs:* Orchestrating AI agents, debugging spread math, and writing the core engine/tests.
  * *2 hrs:* Code review of the planted bugs and finalizing documentation.
