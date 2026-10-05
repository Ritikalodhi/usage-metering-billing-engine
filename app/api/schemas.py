from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


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
