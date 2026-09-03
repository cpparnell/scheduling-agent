"""Lightweight, in-process token/cost accounting for Anthropic API calls.

Each of detector.py, dedup.py, and evals/judge.py calls `record(model,
response.usage)` right after a `messages.create` call. Callers that don't care
(production main.py) pay nothing beyond the call — records just accumulate in
a module-level list until something reads or resets them, most notably the
eval harness (evals/run.py), which resets at the start of a run and reports
the total at the end so every report.json carries its own cost.

Pricing is per-model, $ per 1M tokens, hardcoded here since the API does not
return cost. Keep in sync with providers' published rates when models change.
"""

from dataclasses import dataclass, field

# $ per 1,000,000 tokens (input, output). Keyed by the model-ID prefix so both
# dated snapshots ("claude-haiku-4-5-20251001") and bare IDs match.
_PRICING_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-fable-5": (10.00, 50.00),
    "claude-mythos-5": (10.00, 50.00),
}


def _price_for(model: str) -> tuple[float, float] | None:
    for prefix, price in _PRICING_PER_MTOK.items():
        if model.startswith(prefix):
            return price
    return None


@dataclass
class UsageRecord:
    model: str
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    cost_usd: float | None = None


_records: list[UsageRecord] = []


def record(model: str, usage) -> UsageRecord:
    """Record one API call's usage. `usage` is the `.usage` object off an
    Anthropic response (or anything with the same attributes), or None (a
    stub/fake response in a test, e.g., won't have real usage — recorded as
    zero-token rather than raising). Unpriced models (unrecognized prefix)
    are still recorded with cost_usd=None so totals stay honest about what
    couldn't be costed, rather than silently treating them as free."""
    input_tokens = getattr(usage, "input_tokens", 0) or 0
    output_tokens = getattr(usage, "output_tokens", 0) or 0
    cache_creation = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0

    price = _price_for(model)
    cost_usd = None
    if price is not None:
        in_price, out_price = price
        # Cache writes are billed ~1.25x the base input rate, cache reads
        # ~0.1x; see shared prompt-caching docs. Uncached input at full rate.
        cost_usd = (
            input_tokens * in_price
            + cache_creation * in_price * 1.25
            + cache_read * in_price * 0.1
            + output_tokens * out_price
        ) / 1_000_000

    rec = UsageRecord(
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation,
        cache_read_input_tokens=cache_read,
        cost_usd=cost_usd,
    )
    _records.append(rec)
    return rec


def reset() -> None:
    """Clear all recorded usage. Call at the start of an eval run so the
    resulting totals cover only that run."""
    _records.clear()


def get_records() -> list[UsageRecord]:
    return list(_records)


def summary() -> dict:
    """Aggregate totals: overall + a per-model breakdown, plus a count of
    calls whose model had no known pricing (their cost is excluded from
    total_cost_usd, so `unpriced_calls > 0` means the total is a floor, not
    an exact figure)."""
    total_cost = 0.0
    total_in = 0
    total_out = 0
    unpriced_calls = 0
    by_model: dict[str, dict] = {}

    for rec in _records:
        total_in += rec.input_tokens
        total_out += rec.output_tokens
        if rec.cost_usd is None:
            unpriced_calls += 1
        else:
            total_cost += rec.cost_usd

        m = by_model.setdefault(rec.model, {
            "calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
        })
        m["calls"] += 1
        m["input_tokens"] += rec.input_tokens
        m["output_tokens"] += rec.output_tokens
        if rec.cost_usd is not None:
            m["cost_usd"] += rec.cost_usd

    for m in by_model.values():
        m["cost_usd"] = round(m["cost_usd"], 6)

    return {
        "total_calls": len(_records),
        "unpriced_calls": unpriced_calls,
        "total_input_tokens": total_in,
        "total_output_tokens": total_out,
        "total_cost_usd": round(total_cost, 6),
        "by_model": by_model,
    }
