"""Shared record types written by every node."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class FlagType(StrEnum):
    HIDDEN_TEXT = "hidden_text"
    NO_EXTRACTABLE_TEXT = "no_extractable_text"
    UNSUPPORTED_FILE = "unsupported_file"
    PARSE_FAILED = "parse_failed"
    UNVERIFIED_EVIDENCE = "unverified_evidence"
    UNSTABLE_CRITERION = "unstable_criterion"
    TIER_STRADDLE = "tier_straddle"
    MODEL_REFUSAL = "model_refusal"
    MODEL_ERROR = "model_error"


class Flag(BaseModel):
    node: str
    type: FlagType
    detail: str
    criterion: str | None = None


class TokenUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_input_tokens=self.cache_read_input_tokens + other.cache_read_input_tokens,
            cache_creation_input_tokens=self.cache_creation_input_tokens + other.cache_creation_input_tokens,
        )


class LLMCallRecord(BaseModel):
    """One model call, stored verbatim (principle 6)."""

    model: str
    prompt_hash: str
    request: dict[str, Any]
    raw_response: dict[str, Any]
    stop_reason: str | None
    usage: TokenUsage
    latency_ms: int


class NodeOutput(BaseModel):
    node: str
    input_hash: str
    output: dict[str, Any]
    config: dict[str, Any]
    flags: list[Flag] = Field(default_factory=list)
    llm_calls: list[LLMCallRecord] = Field(default_factory=list)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    latency_ms: int = 0
