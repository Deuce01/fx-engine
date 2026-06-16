"""Data models for the FX engine.

All monetary fields use Decimal.  Pydantic models handle API request
validation; frozen dataclasses represent immutable domain objects.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator

# ─── Constants ────────────────────────────────────────────────────

SUPPORTED_CURRENCIES = {"USD", "EUR", "KES", "NGN"}


# ─── Domain Models ───────────────────────────────────────────────


@dataclass(frozen=True)
class Quote:
    """An immutable FX quote with a locked exchange rate."""

    id: str
    customer_id: str
    from_currency: str
    to_currency: str
    amount: Decimal
    rate: Decimal
    final_amount: Decimal
    created_at: datetime
    expires_at: datetime
    executed: bool = False

    def to_dict(self) -> dict:
        """Serialize for API responses.  All Decimals become strings."""
        return {
            "quote_id": self.id,
            "customer_id": self.customer_id,
            "from_currency": self.from_currency,
            "to_currency": self.to_currency,
            "amount": str(self.amount),
            "rate": str(self.rate),
            "final_amount": str(self.final_amount),
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
        }


@dataclass(frozen=True)
class Transaction:
    """A completed FX transaction record."""

    id: str
    quote_id: str
    customer_id: str
    from_currency: str
    to_currency: str
    amount: Decimal
    final_amount: Decimal
    rate: Decimal
    executed_at: datetime


# ─── API Request Schemas ─────────────────────────────────────────


class CreateCustomerRequest(BaseModel):
    """POST /customers body."""

    name: str = Field(..., min_length=1, max_length=255)


class CreditBalanceRequest(BaseModel):
    """POST /customers/{id}/balances/credit body."""

    currency: str
    amount: str

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, v: str) -> str:
        v = v.upper()
        if v not in SUPPORTED_CURRENCIES:
            raise ValueError(f"currency must be one of {SUPPORTED_CURRENCIES}")
        return v

    @field_validator("amount")
    @classmethod
    def validate_amount(cls, v: str) -> str:
        d = Decimal(v)
        if d <= 0:
            raise ValueError("amount must be positive")
        return v


class CreateQuoteRequest(BaseModel):
    """POST /quotes body."""

    customer_id: str
    from_currency: str
    to_currency: str
    amount: str

    @field_validator("from_currency", "to_currency")
    @classmethod
    def validate_currency(cls, v: str) -> str:
        v = v.upper()
        if v not in SUPPORTED_CURRENCIES:
            raise ValueError(f"currency must be one of {SUPPORTED_CURRENCIES}")
        return v

    @field_validator("amount")
    @classmethod
    def validate_amount(cls, v: str) -> str:
        d = Decimal(v)
        if d <= 0:
            raise ValueError("amount must be positive")
        return v
