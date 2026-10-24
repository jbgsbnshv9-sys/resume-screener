"""Model clients.

Every node talks to models through `LLMClient.complete`. Implementations:

- `AnthropicClient`: the real API.
- `SimulatedClient` (screener.simulated): a deterministic stand-in used for
  offline development and tests. It's selected only by a `simulated-*` model ID
  in the config, so a run's record always says which one produced it.
- `CassetteClient`: records real responses to a file, or replays them, so unit
  tests are deterministic without network access.

Sampling parameters (temperature, top_p, top_k) are not sent: API version
2026-10-21 rejects them on every model. Sample-to-sample variation comes from
default sampling, and `effort` is the tunable.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from screener.hashing import hash_value
from screener.pipeline.types import TokenUsage

Task = Literal["parse", "score"]


class UserBlock(BaseModel):
    text: str
    cache: bool = False


class LLMRequest(BaseModel):
    task: Task
    model: str
    system: str
    user_blocks: list[UserBlock]
    output_schema: dict[str, Any]
    max_tokens: int
    effort: str | None = None
    prompt_hash: str
    # Distinguishes repeated samples of an identical request (for cassettes and the simulator).
    sample_index: int = 0

    def api_params(self) -> dict[str, Any]:
        content: list[dict[str, Any]] = []
        for block in self.user_blocks:
            item: dict[str, Any] = {"type": "text", "text": block.text}
            if block.cache:
                item["cache_control"] = {"type": "ephemeral"}
            content.append(item)
        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": self.output_schema}}
        if self.effort:
            output_config["effort"] = self.effort
        return {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": self.system,
            "messages": [{"role": "user", "content": content}],
            "output_config": output_config,
        }

    def key(self) -> str:
        return hash_value({"params": self.api_params(), "sample": self.sample_index})

    def resume_text(self) -> str:
        """The text inside the <resume> tags (used by the simulator)."""
        first = self.user_blocks[0].text
        start = first.index("<resume>\n") + len("<resume>\n")
        end = first.rindex("\n</resume>")
        return first[start:end]


class LLMResponse(BaseModel):
    model: str
    text: str | None
    parsed: dict[str, Any] | None
    stop_reason: str | None
    raw: dict[str, Any]
    usage: TokenUsage = Field(default_factory=TokenUsage)
    latency_ms: int = 0


class LLMError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(f"{kind}: {message}")
        self.kind = kind


class LLMClient(Protocol):
    def complete(self, request: LLMRequest) -> LLMResponse: ...


ClientProvider = Callable[[str], LLMClient]


def response_from_raw(model: str, raw: dict[str, Any], latency_ms: int) -> LLMResponse:
    texts = [b.get("text", "") for b in raw.get("content", []) if b.get("type") == "text"]
    text = "".join(texts) if texts else None
    stop_reason = raw.get("stop_reason")
    parsed = None
    if text and stop_reason not in ("refusal", "max_tokens"):
        try:
            value = json.loads(text)
            parsed = value if isinstance(value, dict) else None
        except json.JSONDecodeError:
            parsed = None
    u = raw.get("usage") or {}
    usage = TokenUsage(
        input_tokens=u.get("input_tokens") or 0,
        output_tokens=u.get("output_tokens") or 0,
        cache_read_input_tokens=u.get("cache_read_input_tokens") or 0,
        cache_creation_input_tokens=u.get("cache_creation_input_tokens") or 0,
    )
    return LLMResponse(model=raw.get("model", model), text=text, parsed=parsed, stop_reason=stop_reason, raw=raw, usage=usage, latency_ms=latency_ms)


class AnthropicClient:
    """The real API. Credentials resolve from the environment (ANTHROPIC_API_KEY etc.)."""

    def __init__(self, client: Any | None = None, timeout: float = 120.0, max_retries: int = 3):
        import anthropic

        self._anthropic = anthropic
        self._client = client or anthropic.Anthropic(timeout=timeout, max_retries=max_retries)

    def complete(self, request: LLMRequest) -> LLMResponse:
        a = self._anthropic
        started = time.monotonic()
        try:
            message = self._client.messages.create(**request.api_params())
        except a.BadRequestError as e:
            raise LLMError("bad_request", e.message) from e
        except (a.AuthenticationError, a.PermissionDeniedError) as e:
            raise LLMError("auth", e.message) from e
        except a.NotFoundError as e:
            raise LLMError("not_found", e.message) from e
        except a.RateLimitError as e:
            raise LLMError("rate_limited", e.message) from e
        except a.APIStatusError as e:
            raise LLMError(f"status_{e.status_code}", e.message) from e
        except a.APIConnectionError as e:
            raise LLMError("connection", str(e)) from e
        latency_ms = int((time.monotonic() - started) * 1000)
        return response_from_raw(request.model, message.model_dump(mode="json"), latency_ms)


class CassetteClient:
    """Record real responses to a JSON file, or replay them.

    In replay mode a request with no recording raises, so a test can never
    silently fall through to the network.
    """

    def __init__(self, path: Path, mode: Literal["record", "replay"], inner: LLMClient | None = None):
        if mode == "record" and inner is None:
            raise ValueError("record mode needs an inner client")
        self.path = Path(path)
        self.mode = mode
        self.inner = inner
        self._lock = threading.Lock()
        self._entries: dict[str, dict[str, Any]] = json.loads(self.path.read_text()) if self.path.exists() else {}

    def complete(self, request: LLMRequest) -> LLMResponse:
        key = request.key()
        if key in self._entries:
            entry = self._entries[key]
            return response_from_raw(request.model, entry["raw"], entry.get("latency_ms", 0))
        if self.mode == "replay":
            raise LLMError("cassette_miss", f"no recording for {request.task} request {key[:12]}")
        assert self.inner is not None
        response = self.inner.complete(request)
        with self._lock:
            self._entries[key] = {"task": request.task, "raw": response.raw, "latency_ms": response.latency_ms}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._entries, indent=1, sort_keys=True))
        return response


def default_provider(live: bool) -> ClientProvider:
    """Simulated models always resolve to the simulator; real models need `live`."""
    from screener.simulated import SimulatedClient

    cache: dict[str, LLMClient] = {}

    def provide(model: str) -> LLMClient:
        if model not in cache:
            if model.startswith("simulated-"):
                cache[model] = SimulatedClient(model)
            elif live:
                cache[model] = AnthropicClient()
            else:
                raise LLMError("live_disabled", f"model {model} needs live mode (--live / SCREENER_LIVE_LLM=1)")
        return cache[model]

    return provide
