"""Session usage and cost tracking for the TUI dashboard.

The old status rail showed ``tokens_used`` against a budget and nothing else — no
input/output split, no cost, no per-model breakdown, no context pressure.  That is not
enough to make decisions with: "am I about to blow the context window?" and "what has
this session cost me?" are the two questions a user actually asks.

Design rules:

* **Never invent numbers.** Every figure is either *measured* (the provider returned a
  usage block) or *estimated* (we counted characters), and the distinction is carried
  on the record and rendered in the UI.  A confidently wrong cost is worse than an
  honestly labelled estimate.
* **Pricing is data, not belief.** Rates come from the model catalog when the provider
  publishes them (OpenRouter does).  With no rate, cost is ``None`` and the UI says
  "unpriced" rather than showing ``$0.00``.
* **Cheap and thread-safe.** The agent thread records; the UI thread reads snapshots.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

__all__ = ["UsageRecord", "ModelUsage", "UsageSnapshot", "UsageTracker", "estimate_tokens"]


#: Rough character→token ratio for English prose and code.  Only used when a provider
#: gives us no usage block; always surfaced as an estimate.
_CHARS_PER_TOKEN = 3.8


def estimate_tokens(text: str) -> int:
    """Approximate token count from raw text.  Deliberately conservative (rounds up)."""

    if not text:
        return 0
    return max(1, int(len(text) / _CHARS_PER_TOKEN + 0.5))


@dataclass(slots=True)
class UsageRecord:
    """One provider call's cost to the session."""

    model: str = ""
    provider: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    #: True when the provider reported these numbers; False when we counted characters.
    measured: bool = False
    cost_usd: float | None = None
    at: float = field(default_factory=time.time)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(slots=True)
class ModelUsage:
    """Per-model rollup, so a session across two models stays legible."""

    model: str = ""
    provider: str = ""
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    priced: bool = True
    measured: bool = True

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(slots=True)
class UsageSnapshot:
    """An immutable read of the session, safe to hand to the render thread."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    #: False when at least one call had no published price: the total is a floor.
    fully_priced: bool = True
    #: False when at least one figure came from character counting.
    fully_measured: bool = True
    context_tokens: int = 0
    context_window: int = 0
    by_model: tuple[ModelUsage, ...] = ()
    started_at: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def context_pct(self) -> float:
        if self.context_window <= 0:
            return 0.0
        return min(100.0, self.context_tokens / self.context_window * 100.0)

    @property
    def elapsed_s(self) -> float:
        return max(0.0, time.time() - self.started_at) if self.started_at else 0.0

    def cost_text(self) -> str:
        """Cost formatted with its own honesty built in."""

        if self.calls == 0:
            return "$0.00"
        if not self.fully_priced and self.cost_usd <= 0:
            return "unpriced"
        prefix = "" if self.fully_measured else "~"
        suffix = "+" if not self.fully_priced else ""
        if self.cost_usd < 0.01:
            return f"{prefix}${self.cost_usd:.4f}{suffix}"
        return f"{prefix}${self.cost_usd:.3f}{suffix}"

    def tokens_text(self) -> str:
        tilde = "" if self.fully_measured else "~"
        return f"{tilde}{self.input_tokens:,} in · {tilde}{self.output_tokens:,} out"

    def to_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "cost_text": self.cost_text(),
            "fully_priced": self.fully_priced,
            "fully_measured": self.fully_measured,
            "context_tokens": self.context_tokens,
            "context_window": self.context_window,
            "context_pct": round(self.context_pct, 1),
            "elapsed_s": round(self.elapsed_s, 1),
            "by_model": [
                {
                    "model": item.model,
                    "provider": item.provider,
                    "calls": item.calls,
                    "input_tokens": item.input_tokens,
                    "output_tokens": item.output_tokens,
                    "total_tokens": item.total_tokens,
                    "cost_usd": round(item.cost_usd, 6),
                    "priced": item.priced,
                }
                for item in self.by_model
            ],
        }


class UsageTracker:
    """Accumulates usage for one session.

    Prices are per *million* tokens, matching how every vendor publishes them.  Feed
    them with :meth:`set_pricing` (the model catalog knows them for OpenRouter); without
    a price the tracker still counts tokens and reports the total as unpriced.
    """

    def __init__(self, *, context_window: int = 0) -> None:
        self._lock = threading.Lock()
        self._records: list[UsageRecord] = []
        self._pricing: dict[str, tuple[float, float]] = {}
        self._context_tokens = 0
        self._context_window = context_window
        self._started_at = time.time()

    # -- configuration ----------------------------------------------------- #
    def set_pricing(self, model: str, *, input_per_mtok: float, output_per_mtok: float) -> None:
        """Register published rates for a model, in USD per million tokens."""

        with self._lock:
            self._pricing[model] = (input_per_mtok, output_per_mtok)

    def learn_pricing_from_models(self, models: Any) -> int:
        """Absorb prices from :class:`ModelInfo` objects that carry them.

        Returns how many models gained a price.  Providers publish prices per token as
        strings (OpenRouter) or per million as floats; both are normalised here.
        """

        learned = 0
        for item in models or ():
            prompt = _coerce_price(getattr(item, "input_price", None) or getattr(item, "prompt_price", None))
            completion = _coerce_price(getattr(item, "output_price", None) or getattr(item, "completion_price", None))
            model_id = getattr(item, "id", "")
            if model_id and (prompt is not None or completion is not None):
                self.set_pricing(model_id, input_per_mtok=prompt or 0.0, output_per_mtok=completion or 0.0)
                learned += 1
        return learned

    def set_context_window(self, window: int) -> None:
        with self._lock:
            self._context_window = max(0, int(window or 0))

    def set_context_tokens(self, tokens: int) -> None:
        """How much of the window the live conversation currently occupies."""

        with self._lock:
            self._context_tokens = max(0, int(tokens or 0))

    # -- recording --------------------------------------------------------- #
    def record(
        self,
        *,
        model: str,
        provider: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        measured: bool = True,
        cost_usd: float | None = None,
    ) -> UsageRecord:
        """Record one call.  Cost is derived from pricing unless supplied outright."""

        with self._lock:
            if cost_usd is None:
                rates = self._pricing.get(model)
                if rates is not None:
                    cost_usd = (input_tokens * rates[0] + output_tokens * rates[1]) / 1_000_000.0
            record = UsageRecord(
                model=model,
                provider=provider,
                input_tokens=max(0, int(input_tokens)),
                output_tokens=max(0, int(output_tokens)),
                measured=measured,
                cost_usd=cost_usd,
            )
            self._records.append(record)
            return record

    def record_from_payload(self, payload: Any, *, model: str, provider: str = "") -> UsageRecord:
        """Record from a provider's ``usage`` block, falling back to an estimate.

        Accepts OpenAI (``prompt_tokens``/``completion_tokens``), Anthropic
        (``input_tokens``/``output_tokens``) and plain dict shapes.
        """

        usage = payload
        if hasattr(payload, "usage"):
            usage = payload.usage
        if usage is None:
            return self.record(model=model, provider=provider, measured=False)

        def _get(*names: str) -> int:
            for name in names:
                value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
                if isinstance(value, (int, float)) and value >= 0:
                    return int(value)
            return 0

        prompt = _get("prompt_tokens", "input_tokens")
        completion = _get("completion_tokens", "output_tokens")
        measured = bool(prompt or completion)
        return self.record(
            model=model, provider=provider, input_tokens=prompt, output_tokens=completion, measured=measured
        )

    def record_estimated(self, *, model: str, provider: str = "", prompt: str = "", completion: str = "") -> UsageRecord:
        """Last resort: count characters, and mark the record as an estimate."""

        return self.record(
            model=model,
            provider=provider,
            input_tokens=estimate_tokens(prompt),
            output_tokens=estimate_tokens(completion),
            measured=False,
        )

    def reset(self) -> None:
        with self._lock:
            self._records.clear()
            self._context_tokens = 0
            self._started_at = time.time()

    # -- reading ------------------------------------------------------------ #
    def snapshot(self) -> UsageSnapshot:
        with self._lock:
            records = list(self._records)
            window = self._context_window
            context = self._context_tokens
            started = self._started_at
            pricing = dict(self._pricing)

        rollup: dict[str, ModelUsage] = {}
        total_cost = 0.0
        fully_priced = True
        fully_measured = True
        for record in records:
            bucket = rollup.setdefault(record.model, ModelUsage(model=record.model, provider=record.provider))
            bucket.calls += 1
            bucket.input_tokens += record.input_tokens
            bucket.output_tokens += record.output_tokens
            if record.cost_usd is None:
                bucket.priced = False
                if record.model not in pricing:
                    fully_priced = False
            else:
                bucket.cost_usd += record.cost_usd
                total_cost += record.cost_usd
            if not record.measured:
                bucket.measured = False
                fully_measured = False

        return UsageSnapshot(
            calls=len(records),
            input_tokens=sum(item.input_tokens for item in records),
            output_tokens=sum(item.output_tokens for item in records),
            cost_usd=total_cost,
            fully_priced=fully_priced,
            fully_measured=fully_measured,
            context_tokens=context,
            context_window=window,
            by_model=tuple(sorted(rollup.values(), key=lambda item: -item.total_tokens)),
            started_at=started,
        )


def _coerce_price(value: Any) -> float | None:
    """Normalise a published price to USD per million tokens.

    Vendors are inconsistent: OpenRouter sends per-token strings like ``"0.000003"``,
    others send per-million floats.  Anything below 0.001 is assumed per-token.
    """

    if value in (None, "", "0"):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return number * 1_000_000.0 if number < 0.001 else number
