# DECISIONS.md — Architecture & AI Process Documentation

## Architectural Trade-offs & Decisions

### 1. Concurrency Model: SQLite `WAL` + `BEGIN IMMEDIATE`
We faced a critical decision regarding how to prevent the N-parallel execution race conditions. The typical naive AI response is to use a `threading.Lock()` or an in-memory `asyncio.Lock()`.
- **Rejected:** `threading.Lock()` is a toy solution. It fails completely the moment the application is scaled to multiple processes (e.g., `uvicorn --workers 4`) or deployed across multiple Kubernetes pods.
- **Accepted:** We pushed the concurrency control to the database layer. By using SQLite in `WAL` mode and executing quotes inside a manual `BEGIN IMMEDIATE` transaction, SQLite natively serializes writes. This completely eliminates the race condition, scales naturally to multiple API processes on the same machine, and ensures perfectly atomic rollbacks if a balance is insufficient. 

### 2. Financial Arithmetic: The Anti-Float Rule
- AI coding assistants aggressively default to Python's `float` type for anything involving decimals.
- **Decision:** We strictly enforced a `No Float` policy in the prompt. `decimal.Decimal` is used exclusively.
- **Rounding Strategy:** To prevent rounding errors from compounding during intermediate calculations (e.g., crossing `KES -> USD -> NGN`), we maintain high precision internally and apply `ROUND_HALF_UP` quantized to `0.01` *exactly once*, right before persistence. 

### 3. Rate Resolution: Spread Protection
- **Decision:** The engine must protect the bank's 50bps spread regardless of the direction of the trade.
- If a customer converts Base -> Quote, the bank buys Base, so we apply the `buy` rate.
- If a customer converts Quote -> Base, the bank sells Base, so we apply `1 / sell`.
- **Catching an AI Error:** Initially, the AI implemented `1 / buy` for inverse pairs, which eroded the spread and caused the bank to lose money on round-trip conversions. This was caught by a property-based test and fixed.

---

## AI Collaboration & Process

### Prompt Strategies Used
1. **Spec-Driven Development:** Instead of asking the AI to "write an FX engine," we first spent an hour having the AI write a bulletproof `SPEC.md`. We then used that spec as the primary prompt for all subsequent code generation. This anchored the AI to specific business rules and prevented drift.
2. **Constraint Prompts:** The `AGENTS.md` file (which contains our system prompt constraints) was hyper-focused on *what not to do*. Explicitly forbidding `float` and `threading.Lock` forced the AI to generate production-grade solutions.
3. **Property-Based Testing (Hypothesis):** Instead of writing 5 hardcoded unit tests, we asked the AI to write `Hypothesis` tests that slammed the engine with thousands of random amounts across all 12 currency pairs to prove mathematically that precision was maintained.

### Dead-Ends & AI Hallucinations
1. **The Roundtrip Money Glitch:** As mentioned above, the AI's initial implementation of inverse pair logic mathematically benefited the customer. The Hypothesis test caught a scenario where converting `10.00 USD -> EUR -> USD` resulted in `10.10 USD`. The AI struggled to reason through the Bid/Ask spread mechanics until manually directed to consider "who is buying what."
2. **Function-Scoped Fixture Warnings:** The AI generated `Hypothesis` tests using `pytest` fixtures, triggering health check warnings because fixtures aren't reset between Hypothesis examples. We had to manually prompt the AI to suppress `HealthCheck.function_scoped_fixture`.

### Effective Tools
- **DeepMind's Agentic Assistant (Antigravity):** Exceptional at generating the boilerplate and executing the `SPEC.md` flawlessly.
- **Pytest + Hypothesis:** The absolute best tools for catching edge-case math errors that standard unit tests miss.
- **Git:** We used atomic commits specifically separating the database layer, the engine, and the API, which made it much easier to isolate where an AI-generated bug originated.
