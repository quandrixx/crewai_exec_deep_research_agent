"""
Per-stage token accounting and cost estimation.

Exists because "how much does a run cost?" was unanswerable from the outside.
Attributing spend to a stage meant counting tool calls in a verbose log and
estimating prompt sizes by hand, which produced an answer off by a factor of
three. CrewAI already tracks real usage on every CrewOutput; this module
records it, prices it, and puts the number in front of whoever ran the
pipeline.

Measured baseline (enhanced geothermal systems, clean run, no revision round):
**$0.49** - research $0.30, analysis $0.08, report $0.10.

**Why research dominates.** An agent loop resends its entire accumulated
conversation on every iteration, so a stage making N tool calls pays input
tokens proportional to N-squared, not N. Measured runs made 38-42 tool
executions, against tasks asking for "4-6 SEPARATE, NARROW searches".
Over-searching is therefore far more expensive than it looks: halving the tool
calls cuts cost by roughly four times, not two. When a change moves the total,
this is almost always why.

**A caching observation worth acting on.** A measured run showed the research
stage reading 35,783 tokens from cache, but the analysis and report stages
reading zero while writing ~13k each. Those two crews run once per pipeline
with two differently-prompted tasks, so they pay the 1.25x cache-WRITE premium
on almost every input token and never read any of it back. Cache writes only
pay off on reuse; without it they are a straight 25% surcharge.

**On the estimate's accuracy.** Rates below are list prices from Anthropic's
model documentation, verified 2026-08-21. The output is an estimate, not a
bill: it can't see negotiated discounts, and it depends on how the provider
reports cached tokens (documented on `_stage_cost`) - `sanity_notes` flags a
run that contradicts it.
"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ModelPricing:
    """List price per million tokens, plus Anthropic's prompt-caching multipliers."""

    input_per_mtok: float
    output_per_mtok: float
    # Cache reads bill at ~0.1x the base input rate; writes at 1.25x for the
    # default 5-minute TTL. Nothing here enables caching - these apply only if
    # something upstream does.
    cache_read_multiplier: float = 0.1
    cache_write_multiplier: float = 1.25


# Keyed by the model id CrewAI reports on `agent.llm.model` - the provider
# prefix ("anthropic/") is already stripped by the time it lands there.
# Verified against Anthropic's models overview on 2026-08-21.
PRICING: dict[str, ModelPricing] = {
    "claude-opus-5": ModelPricing(5.00, 25.00),
    "claude-sonnet-5": ModelPricing(2.00, 10.00),
    "claude-sonnet-4-5": ModelPricing(3.00, 15.00),
    "claude-sonnet-4-6": ModelPricing(3.00, 15.00),
    "claude-haiku-4-5": ModelPricing(1.00, 5.00),
}


@dataclass
class StageUsage:
    """What one crew consumed, and what that costs."""

    stage: str
    models: list[str]
    prompt_tokens: int = 0
    cached_prompt_tokens: int = 0
    cache_creation_tokens: int = 0
    completion_tokens: int = 0
    successful_requests: int = 0
    tool_calls: int | None = None

    @property
    def priced_model(self) -> str | None:
        """The model used to price this stage.

        Each crew in this project runs a single model, so the first is the
        right one. A mixed-model crew is flagged rather than silently priced
        at whichever model happened to sort first.
        """
        return self.models[0] if self.models else None

    @property
    def cost_usd(self) -> float | None:
        """Estimated cost, or None when the model has no known price."""
        model = self.priced_model
        if model is None or model not in PRICING:
            return None
        return _stage_cost(self, PRICING[model])

    @property
    def sanity_notes(self) -> list[str]:
        """Anything that would make the estimate untrustworthy.

        Surfaced rather than swallowed: a cost number with a silently wrong
        assumption behind it is worse than no number at all.
        """
        notes: list[str] = []
        if not self.models:
            notes.append(f"{self.stage}: no model recorded; stage not priced")
        elif len(set(self.models)) > 1:
            notes.append(
                f"{self.stage}: mixed models {sorted(set(self.models))}; "
                f"priced entirely at {self.priced_model}"
            )
        if self.priced_model and self.priced_model not in PRICING:
            notes.append(
                f"{self.stage}: no price known for '{self.priced_model}'; "
                f"add it to costs.PRICING"
            )
        accounted = self.cached_prompt_tokens + self.cache_creation_tokens
        if accounted > self.prompt_tokens:
            notes.append(
                f"{self.stage}: cached + cache-creation tokens ({accounted:,}) exceed "
                f"prompt_tokens ({self.prompt_tokens:,}) - the assumption that "
                f"prompt_tokens is inclusive of both is wrong for this provider, "
                f"so the estimate understates cost"
            )
        return notes


def _stage_cost(usage: StageUsage, pricing: ModelPricing) -> float:
    """Price one stage's usage.

    `prompt_tokens` is the TOTAL input, inclusive of both the cache-read and
    cache-write portions - so the full-rate remainder is what's left after
    subtracting BOTH. Verified against a real run: the analysis stage reported
    13,587 prompt tokens and 13,581 cache-creation tokens, which only makes
    sense if the latter is a subset of the former. Subtracting only the cached
    reads (the first version of this) double-charged every cache-write token,
    inflating the run's estimate by about 25%.

    Clamped at zero so a provider that reports these separately under-reports
    rather than producing a negative charge; `sanity_notes` flags that case.
    """
    uncached = max(
        usage.prompt_tokens - usage.cached_prompt_tokens - usage.cache_creation_tokens,
        0,
    )

    per_token_in = pricing.input_per_mtok / 1_000_000
    per_token_out = pricing.output_per_mtok / 1_000_000

    return (
        uncached * per_token_in
        + usage.cached_prompt_tokens * per_token_in * pricing.cache_read_multiplier
        + usage.cache_creation_tokens * per_token_in * pricing.cache_write_multiplier
        + usage.completion_tokens * per_token_out
    )


class UsageLedger:
    """Accumulates per-stage usage for a single pipeline run.

    A module-level instance (`LEDGER` below) is used rather than threading a
    ledger through the Flow and every crew, which would mean changing five
    signatures to carry something orthogonal to what they do. The tradeoff is
    process-wide state: it is scoped to one run, and `main.py` calls `reset()`
    before kicking off. Anything running two pipelines concurrently in one
    process would need to revisit this.
    """

    def __init__(self) -> None:
        self.stages: list[StageUsage] = []

    def reset(self) -> None:
        self.stages = []

    def record(self, stage: str, crew: Any, result: Any, tool_calls: int | None = None) -> None:
        """Record one crew's usage from its CrewOutput.

        Tolerates missing usage rather than raising: cost reporting must never
        be the thing that breaks a run that otherwise succeeded.
        """
        usage = getattr(result, "token_usage", None)
        models = [
            model
            for agent in getattr(crew, "agents", [])
            if (model := getattr(getattr(agent, "llm", None), "model", None))
        ]

        self.stages.append(StageUsage(
            stage=stage,
            models=models,
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            cached_prompt_tokens=getattr(usage, "cached_prompt_tokens", 0) or 0,
            cache_creation_tokens=getattr(usage, "cache_creation_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            successful_requests=getattr(usage, "successful_requests", 0) or 0,
            tool_calls=tool_calls,
        ))

    @property
    def total_cost_usd(self) -> float | None:
        """Total across stages, or None if no stage could be priced."""
        priced = [s.cost_usd for s in self.stages if s.cost_usd is not None]
        return sum(priced) if priced else None

    @property
    def notes(self) -> list[str]:
        return [note for stage in self.stages for note in stage.sanity_notes]

    def as_dict(self) -> dict[str, Any]:
        """Serializable form, for saving alongside the run's other artifacts."""
        return {
            "total_cost_usd": self.total_cost_usd,
            "notes": self.notes,
            "stages": [
                {
                    "stage": s.stage,
                    "model": s.priced_model,
                    "prompt_tokens": s.prompt_tokens,
                    "cached_prompt_tokens": s.cached_prompt_tokens,
                    "cache_creation_tokens": s.cache_creation_tokens,
                    "completion_tokens": s.completion_tokens,
                    "requests": s.successful_requests,
                    "cost_usd": s.cost_usd,
                }
                for s in self.stages
            ],
        }

    def render(self) -> list[str]:
        """Human-readable summary lines for the CLI."""
        if not self.stages:
            return ["  cost       : not recorded"]

        total = self.total_cost_usd
        lines = [
            f"  cost       : {_money(total)} estimated"
            + ("" if total is not None else " (no stage could be priced)")
        ]
        for s in self.stages:
            detail = (
                f"{s.prompt_tokens:,} in / {s.completion_tokens:,} out, "
                f"{s.successful_requests} requests"
            )
            if s.cached_prompt_tokens:
                detail += f", {s.cached_prompt_tokens:,} cached"
            lines.append(
                f"    {s.stage:<10} {_money(s.cost_usd):>8}  "
                f"{s.priced_model or 'unknown model'}  ({detail})"
            )
        for note in self.notes:
            lines.append(f"    NOTE: {note}")
        return lines


def _money(amount: float | None) -> str:
    return "n/a" if amount is None else f"${amount:,.2f}"


# Process-wide, scoped to one pipeline run. See UsageLedger's docstring.
LEDGER = UsageLedger()
