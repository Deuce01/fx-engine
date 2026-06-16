# AGENTS.md — AI Tool Constraints and Instructions

This file documents the system prompt instructions, context bounds, and "rules of engagement" used to guide the AI coding assistant (Agent) during the development of this FX engine.

## Core Prompt Constraints

To ensure the AI generated production-ready code that adhered to strict financial invariants, the following explicit constraints were injected into its context:

### 1. The Anti-Float Rule
> **Constraint:** "You are building a financial engine. The use of the `float` data type is strictly forbidden anywhere in the `src/` or `tests/` directory. You must use `decimal.Decimal` for all monetary amounts and exchange rates. Apply `ROUND_HALF_UP` quantized to two decimal places only at the very final output step before persisting to the database or returning to the client."

*Why:* AI assistants trained on GitHub data overwhelmingly default to using floats for math. This constraint aggressively forces the AI out of its lazy default state.

### 2. The Distributed Concurrency Rule
> **Constraint:** "Assume this FastAPI application will be deployed across 10 different Kubernetes pods simultaneously. Therefore, you are forbidden from using `threading.Lock()` or `asyncio.Lock()` to solve race conditions, as they only protect memory within a single process. All concurrency control must be handled natively by the database using atomic transactions."

*Why:* When asked to fix a double-execution bug, an AI will almost always reach for `threading.Lock()`. This constraint forces the AI to think in terms of database locks and multi-process architectures.

### 3. The Idempotency Imperative
> **Constraint:** "Every state-mutating endpoint must support an `Idempotency-Key` header. Quote execution must be strictly idempotent to safely handle client retries without ever causing a double-debit."

*Why:* Idempotency is a senior-level concept that AIs often omit unless explicitly instructed to include it.

### 4. Code Isolation Rule
> **Constraint:** "You will review the buggy code provided in `planted_bugs/` to understand the domain, but you are forbidden from importing, copying, or modifying that code. You must build a pristine, brand-new architecture from scratch in `src/`."

*Why:* We wanted the final repository to showcase a flawless architecture, rather than an architecture constrained by the bad decisions of the provided buggy baseline.

---

## Agent Setup & Workflows

**Role:** DeepMind Antigravity AI Assistant 
**Mode:** Agentic Planning & Execution
**Process:**
1. The Agent was first instructed to read `ASSIGNMENT.md` and the buggy baseline.
2. The Agent drafted an `implementation_plan.md` and `SPEC.md` for human review.
3. Upon human approval of the spec, the Agent was unleashed to write the codebase autonomously.
4. When tests failed (e.g., the roundtrip spread bug), the human acted as a senior reviewer, pointing out the logical flaw in the AI's spread mathematics and directing it to correct the engine logic.
