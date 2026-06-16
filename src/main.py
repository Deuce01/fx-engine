"""FastAPI application for the FX engine.

Provides REST endpoints for customer management, FX quotes,
quote execution, rate management, and observability.

Structured JSON logging with correlation IDs is attached to every
request for end-to-end tracing (SPEC.md §10).
"""
from __future__ import annotations

import json
import logging
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from .database import get_read_connection, init_db
from .engine import FXEngine, RateProvider
from .models import (
    CreateCustomerRequest,
    CreateQuoteRequest,
    CreditBalanceRequest,
)


# ─── Structured JSON Logging ─────────────────────────────────────


class JSONFormatter(logging.Formatter):
    """Emit structured JSON log lines with correlation IDs."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Merge structured extra fields when present
        for key in (
            "correlation_id",
            "quote_id",
            "customer_id",
            "transaction_id",
            "pair",
            "amount",
            "rate",
            "final_amount",
            "currency",
            "idempotency_key",
            "event",
        ):
            val = getattr(record, key, None)
            if val is not None:
                entry[key] = val
        if record.exc_info and record.exc_info[0]:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry)


handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JSONFormatter())
logging.basicConfig(level=logging.INFO, handlers=[handler])
logger = logging.getLogger(__name__)


# ─── Application Bootstrap ───────────────────────────────────────

rate_provider = RateProvider()
engine = FXEngine(rate_provider)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize database on startup."""
    init_db()
    logger.info("FX engine started")
    yield
    logger.info("FX engine shutting down")


app = FastAPI(
    title="FX Engine",
    description="Production-ready foreign exchange engine with atomic execution",
    version="1.0.0",
    lifespan=lifespan,
)


# ─── Middleware ───────────────────────────────────────────────────


@app.middleware("http")
async def correlation_id_middleware(request: Request, call_next):
    """Attach a correlation ID to every request/response for tracing."""
    correlation_id = request.headers.get("X-Request-Id") or str(uuid.uuid4())
    request.state.correlation_id = correlation_id

    response = await call_next(request)
    response.headers["X-Correlation-Id"] = correlation_id

    # Warn consumers when rates are stale (SPEC.md §9)
    if rate_provider.is_stale():
        response.headers["X-Rates-Stale"] = "true"

    return response


# ─── Global Exception Handler ────────────────────────────────────


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    cid = getattr(request.state, "correlation_id", "-")
    logger.exception("unhandled exception", extra={"correlation_id": cid})
    return JSONResponse(
        status_code=500,
        content={"error": "internal_error", "correlation_id": cid},
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Customer Endpoints
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@app.post("/customers", status_code=201)
async def create_customer(req: CreateCustomerRequest):
    """Create a new customer with zero balances in all currencies."""
    return engine.create_customer(req.name)


@app.get("/customers/{customer_id}/balances")
async def get_balances(customer_id: str):
    """View balances per currency for a customer."""
    try:
        return engine.get_balances(customer_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/customers/{customer_id}/balances/credit")
async def credit_balance(customer_id: str, req: CreditBalanceRequest):
    """Manually credit a customer balance (test fixture)."""
    try:
        amount = Decimal(req.amount)
        return engine.credit_balance(customer_id, req.currency, amount)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except InvalidOperation:
        raise HTTPException(status_code=400, detail="invalid amount format")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Quote Endpoints
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@app.post("/quotes", status_code=201)
async def create_quote(req: CreateQuoteRequest):
    """Generate an FX quote with a locked rate (valid for 60 s)."""
    try:
        amount = Decimal(req.amount)
        quote = engine.generate_quote(
            req.customer_id,
            req.from_currency,
            req.to_currency,
            amount,
        )
        return quote.to_dict()
    except ValueError as exc:
        status = 503 if "critically stale" in str(exc) else 400
        raise HTTPException(status_code=status, detail=str(exc))
    except InvalidOperation:
        raise HTTPException(status_code=400, detail="invalid amount format")


@app.post("/quotes/{quote_id}/execute")
async def execute_quote(
    quote_id: str,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
):
    """Execute an FX quote atomically (debit + credit in one transaction)."""
    try:
        return engine.execute_quote(quote_id, idempotency_key=idempotency_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Rate Endpoints
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@app.post("/rates/refresh")
async def refresh_rates():
    """Trigger a rate refresh from the upstream provider."""
    result = rate_provider.refresh()
    status = 200 if result["status"] == "ok" else 503
    return JSONResponse(content=result, status_code=status)


@app.get("/rates")
async def get_rates():
    """Return current buy/sell rates for all known pairs."""
    return rate_provider.snapshot()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Observability Endpoints (SPEC.md §10)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@app.get("/healthz")
async def health_check():
    """Liveness / readiness probe.

    Checks:
      1. Database is reachable.
      2. Rates are not critically stale (> 15 min).
    """
    issues: list[str] = []

    try:
        with get_read_connection() as conn:
            conn.execute("SELECT 1")
    except Exception as exc:
        issues.append(f"database unreachable: {exc}")

    if rate_provider.is_critically_stale():
        issues.append(
            f"rates critically stale "
            f"({int(rate_provider.staleness_seconds())}s old, "
            f"threshold: 900s)"
        )

    if issues:
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "issues": issues},
        )

    return {
        "status": "healthy",
        "rates_age_seconds": int(rate_provider.staleness_seconds()),
        "rates_last_updated": rate_provider.last_updated_iso(),
    }


@app.get("/metrics")
async def get_metrics():
    """Operational metrics for dashboards and alerting."""
    return {
        "quotes_created": engine.metrics["quotes_created"],
        "quotes_executed": engine.metrics["quotes_executed"],
        "quotes_expired": engine.metrics["quotes_expired"],
        "execution_errors": engine.metrics["execution_errors"],
        "rate_refresh_failures": engine.metrics["rate_refresh_failures"],
        "rates_age_seconds": int(rate_provider.staleness_seconds()),
        "rates_last_updated": rate_provider.last_updated_iso(),
    }
