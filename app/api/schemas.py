from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_tokens: int = Field(default=0, ge=0, le=10_000_000)
    cached_input_tokens: int = Field(default=0, ge=0, le=10_000_000)
    output_tokens: int = Field(default=0, ge=0, le=10_000_000)
    reasoning_tokens: int = Field(default=0, ge=0, le=10_000_000)


class TokensCostBreakdown(BaseModel):
    input: int
    cached_input: int
    output: int
    total_micro_cents: int


class CostBreakdown(BaseModel):
    api_call_micro_cents: int
    tokens: TokensCostBreakdown
    total_micro_cents: int


class UsageStats(BaseModel):
    api_calls: int
    tokens: int


class GenerateResponse(BaseModel):
    usage_event_ids: list[int]
    cost: CostBreakdown
    used: UsageStats
    remaining: UsageStats


# ── Phase 4: GET /v1/usage ────────────────────────────────────────────────────

_MICRO_CENTS_PER_CENT = Decimal("1000000")
_CENTS_PER_DOLLAR = Decimal("100")


class TokensByCategory(BaseModel):
    """Per-category token quantities (raw quantity, not cost)."""

    input: int
    cached_input: int
    output: int
    reasoning: int


class ApiCallsUsage(BaseModel):
    used: int
    limit: int


class TokensUsage(BaseModel):
    used: int
    limit: int
    by_category: TokensByCategory


class UsageResponse(BaseModel):
    """Response for GET /v1/usage (DESIGN.md section 6).

    ``cost_micro_cents`` is an integer.
    ``cost_usd`` is a display-only string produced with Decimal arithmetic — no
    floats are used anywhere in the calculation.
    """

    plan: str
    period: str
    api_calls: ApiCallsUsage
    tokens: TokensUsage
    cost_micro_cents: int
    cost_usd: str

    @model_validator(mode="before")
    @classmethod
    def compute_cost_usd(cls, values: dict) -> dict:
        """Derive ``cost_usd`` from ``cost_micro_cents`` using Decimal math."""
        if "cost_usd" not in values or values.get("cost_usd") is None:
            micro = Decimal(int(values["cost_micro_cents"]))
            dollars = (micro / _MICRO_CENTS_PER_CENT / _CENTS_PER_DOLLAR).quantize(
                Decimal("0.000001"), rounding=ROUND_HALF_UP
            )
            values["cost_usd"] = str(dollars)
        return values
