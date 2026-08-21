"""
test_costs.py

Tests the token accounting and cost estimation.

Two things matter more than the arithmetic. First, an unknown or unpriced model
must never be silently costed at zero — a cost report that quietly omits a
stage is worse than one that says it cannot price it. Second, the estimate
rests on one assumption about CrewAI's accounting (that `prompt_tokens`
includes the cached portion); when a real run contradicts it, that has to
surface rather than skew the number downward.
"""

import pytest  # pyrefly: ignore

from crewai_exec_deep_research_agent.costs import (
    PRICING,
    ModelPricing,
    StageUsage,
    UsageLedger,
)


class StubUsage:
    def __init__(self, prompt=0, cached=0, cache_creation=0, completion=0, requests=0):
        self.prompt_tokens = prompt
        self.cached_prompt_tokens = cached
        self.cache_creation_tokens = cache_creation
        self.completion_tokens = completion
        self.successful_requests = requests


class StubAgent:
    def __init__(self, model):
        self.llm = type("LLM", (), {"model": model})()


class StubCrew:
    def __init__(self, *models):
        self.agents = [StubAgent(m) for m in models]


class StubResult:
    def __init__(self, usage):
        self.token_usage = usage


# ---------------------------------------------------------------------------
# Pricing arithmetic
# ---------------------------------------------------------------------------

def test_uncached_cost_is_rate_times_tokens():
    usage = StageUsage(
        stage="research", models=["claude-sonnet-5"],
        prompt_tokens=1_000_000, completion_tokens=100_000,
    )
    # $2/MTok in + $10/MTok out
    assert usage.cost_usd == pytest.approx(2.00 + 1.00)


def test_cached_input_is_billed_at_the_read_rate():
    """Cache reads bill at ~0.1x, which is the whole point of enabling them."""
    usage = StageUsage(
        stage="s", models=["claude-sonnet-5"],
        prompt_tokens=1_000_000, cached_prompt_tokens=1_000_000,
    )
    assert usage.cost_usd == pytest.approx(0.20)  # 2.00 * 0.1


def test_cache_writes_carry_their_premium():
    usage = StageUsage(
        stage="s", models=["claude-sonnet-5"], cache_creation_tokens=1_000_000,
    )
    assert usage.cost_usd == pytest.approx(2.50)  # 2.00 * 1.25


def test_sonnet_4_5_is_priced_higher_than_sonnet_5():
    """Worth pinning: the analysis and report crews run 4-5 for structured-output
    reliability, and it costs 50% more per token than the newer model the
    research crew uses. If that ever stops being true, revisit the model choice."""
    assert PRICING["claude-sonnet-4-5"].input_per_mtok > PRICING["claude-sonnet-5"].input_per_mtok
    assert PRICING["claude-sonnet-4-5"].output_per_mtok > PRICING["claude-sonnet-5"].output_per_mtok


# ---------------------------------------------------------------------------
# Never silently price at zero
# ---------------------------------------------------------------------------

def test_unknown_model_is_unpriced_and_says_so():
    usage = StageUsage(stage="s", models=["some-future-model"], prompt_tokens=999_999)
    assert usage.cost_usd is None
    assert any("no price known" in note for note in usage.sanity_notes)


def test_stage_with_no_model_is_unpriced_and_says_so():
    usage = StageUsage(stage="s", models=[], prompt_tokens=999_999)
    assert usage.cost_usd is None
    assert any("no model recorded" in note for note in usage.sanity_notes)


def test_mixed_model_crew_is_flagged():
    """Each crew here runs one model. If that changes, the single-rate estimate
    silently stops being right, so it has to announce itself."""
    usage = StageUsage(stage="s", models=["claude-sonnet-5", "claude-haiku-4-5"])
    assert any("mixed models" in note for note in usage.sanity_notes)


def test_cached_exceeding_prompt_tokens_is_flagged():
    """The estimate assumes prompt_tokens includes both the cached-read and
    cache-write portions. If a provider reports them separately this
    understates cost - surface it rather than quietly reporting a low number."""
    usage = StageUsage(
        stage="s", models=["claude-sonnet-5"],
        prompt_tokens=1_000, cached_prompt_tokens=5_000,
    )
    assert any("exceed prompt_tokens" in note for note in usage.sanity_notes)
    # And it must not go negative.
    assert usage.cost_usd >= 0


def test_cache_write_tokens_are_not_double_charged():
    """Regression test for a real bug in this module.

    `prompt_tokens` is inclusive of `cache_creation_tokens` - measured on a live
    run where a stage reported 13,587 prompt and 13,581 cache-creation tokens.
    The first version subtracted only the cached READS, so every cache-write
    token was billed twice (once at full rate, once at the 1.25x premium),
    inflating the run's estimate by ~25%.
    """
    usage = StageUsage(
        stage="s", models=["claude-sonnet-5"],
        prompt_tokens=1_000_000, cache_creation_tokens=1_000_000,
    )
    # Purely a cache write: 1M * $2 * 1.25 = $2.50, not $2.50 + another $2.00.
    assert usage.cost_usd == pytest.approx(2.50)


def test_mixed_cached_uncached_and_written_tokens_partition_correctly():
    usage = StageUsage(
        stage="s", models=["claude-sonnet-5"],
        prompt_tokens=1_000_000,
        cached_prompt_tokens=400_000,
        cache_creation_tokens=400_000,
    )
    # 200k full ($0.40) + 400k read ($0.08) + 400k write ($1.00)
    assert usage.cost_usd == pytest.approx(0.40 + 0.08 + 1.00)


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

def test_ledger_records_and_totals_across_stages():
    ledger = UsageLedger()
    ledger.record("research", StubCrew("claude-sonnet-5"),
                  StubResult(StubUsage(prompt=1_000_000, completion=100_000, requests=42)))
    ledger.record("analysis", StubCrew("claude-sonnet-4-5"),
                  StubResult(StubUsage(prompt=1_000_000, completion=100_000, requests=3)))

    assert len(ledger.stages) == 2
    # research: 2.00 + 1.00 = 3.00 | analysis: 3.00 + 1.50 = 4.50
    assert ledger.total_cost_usd == pytest.approx(7.50)


def test_ledger_reset_clears_stages():
    """Scoping is per-run; a second run in one process must not inherit totals."""
    ledger = UsageLedger()
    ledger.record("research", StubCrew("claude-sonnet-5"), StubResult(StubUsage(prompt=10)))
    ledger.reset()
    assert ledger.stages == []
    assert ledger.total_cost_usd is None


def test_missing_usage_does_not_break_the_run():
    """Cost reporting must never be the thing that fails an otherwise good run."""
    ledger = UsageLedger()
    ledger.record("research", StubCrew("claude-sonnet-5"), object())

    assert ledger.stages[0].prompt_tokens == 0
    assert ledger.total_cost_usd == pytest.approx(0.0)


def test_total_is_none_when_nothing_could_be_priced():
    ledger = UsageLedger()
    ledger.record("research", StubCrew("some-future-model"), StubResult(StubUsage(prompt=10)))
    assert ledger.total_cost_usd is None


def test_rendered_summary_names_stage_model_and_cost():
    ledger = UsageLedger()
    ledger.record("research", StubCrew("claude-sonnet-5"),
                  StubResult(StubUsage(prompt=1_000_000, completion=100_000, requests=42)))
    rendered = "\n".join(ledger.render())

    assert "research" in rendered
    assert "claude-sonnet-5" in rendered
    assert "$3.00" in rendered
    assert "42 requests" in rendered


def test_rendered_summary_surfaces_notes():
    ledger = UsageLedger()
    ledger.record("research", StubCrew("some-future-model"), StubResult(StubUsage(prompt=10)))
    rendered = "\n".join(ledger.render())
    assert "NOTE:" in rendered and "no price known" in rendered


def test_serialized_form_round_trips_the_numbers():
    ledger = UsageLedger()
    ledger.record("research", StubCrew("claude-sonnet-5"),
                  StubResult(StubUsage(prompt=1_000, completion=500, requests=2)))
    data = ledger.as_dict()

    assert data["stages"][0]["stage"] == "research"
    assert data["stages"][0]["model"] == "claude-sonnet-5"
    assert data["stages"][0]["prompt_tokens"] == 1_000
    assert data["total_cost_usd"] == pytest.approx(data["stages"][0]["cost_usd"])
