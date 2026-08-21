"""
test_analysis_guardrails.py

Tests the Analysis Crew's deterministic output guardrails.

These sit one layer earlier than the Flow's citation gate and check different
things. A guardrail only ever sees the TaskOutput, never the task's inputs, so
it cannot know whether claim index 47 exists - that's citation_check_tool's job,
and a guardrail failure is cheap (one task retries) where a gate failure is
expensive (the whole crew re-runs). So the split is: anything decidable from
the output alone is caught here.

The style checks are deliberately loose. knowledge/style_guide.md asks for 3-5
sentences and 2-4 recommendations; the guardrail's bands are wider, because a
deterministic gate should catch a wall of text or a single throwaway line
without policing prose. Two tests below pin that looseness on purpose.
"""

import pytest  # pyrefly: ignore
from crewai.tasks.task_output import TaskOutput

from crewai_exec_deep_research_agent.crews.analysis_crew.analysis_guardrails import (
    _count_sentences,
    validate_investment_judgment,
    validate_landscape_analysis,
)
from crewai_exec_deep_research_agent.models import (
    CompanyProfile,
    EntityType,
    FundingEvent,
    FundingStage,
    InvestmentJudgment,
    LandscapeAnalysis,
    MarketShift,
    Recommendation,
    RecommendationAction,
)


def output(parsed) -> TaskOutput:
    return TaskOutput(description="d", raw="{}", agent="A", pydantic=parsed)


def good_landscape(**overrides) -> LandscapeAnalysis:
    return LandscapeAnalysis(**{
        "market_shifts": [MarketShift(description="A real shift.", supporting_claim_indices=[0])],
        "incumbents": [CompanyProfile(
            name="Co", entity_type=EntityType.INCUMBENT,
            differentiation="Uses a distinct approach.", supporting_claim_indices=[1],
        )],
        "new_entrants": [],
        "funding_events": [],
        **overrides,
    })


def good_judgment(**overrides) -> InvestmentJudgment:
    return InvestmentJudgment(**{
        "executive_summary": "Investable now. Timing favors entry. Regulatory risk dominates.",
        "tensions_or_conflicts": [],
        "recommendations": [Recommendation(
            action=RecommendationAction.MONITOR,
            text="Monitor for two quarters; licensing slippage is the risk.",
            supporting_claim_indices=[0],
        )],
        **overrides,
    })


# ---------------------------------------------------------------------------
# Landscape
# ---------------------------------------------------------------------------

def test_well_formed_landscape_passes():
    passed, result = validate_landscape_analysis(output(good_landscape()))
    assert passed is True
    assert isinstance(result, TaskOutput)


def test_unparsed_landscape_output_restates_the_schema():
    passed, feedback = validate_landscape_analysis(output(None))
    assert passed is False
    assert "market_shifts" in feedback and "funding_events" in feedback


def test_empty_market_shifts_is_rejected():
    """'What's Changing' is a required report section; an empty list means the
    briefing has nothing to say about why this sector matters now."""
    passed, feedback = validate_landscape_analysis(output(good_landscape(market_shifts=[])))
    assert passed is False
    assert "market_shifts is empty" in feedback


def test_company_without_differentiation_is_rejected():
    """The style guide's most-repeated complaint: 'is active in this space' is
    not a profile."""
    bad = good_landscape(incumbents=[CompanyProfile(
        name="Co", entity_type=EntityType.INCUMBENT,
        differentiation="   ", supporting_claim_indices=[0],
    )])
    passed, feedback = validate_landscape_analysis(output(bad))
    assert passed is False
    assert "no differentiation" in feedback


def test_entry_citing_no_claims_is_rejected():
    bad = good_landscape(market_shifts=[
        MarketShift(description="Unsupported shift.", supporting_claim_indices=[]),
    ])
    passed, feedback = validate_landscape_analysis(output(bad))
    assert passed is False
    assert "cites no supporting claims" in feedback


def test_negative_claim_index_is_rejected():
    """Range-checking against the real claim list belongs to the Flow's gate,
    but a negative index is decidable here and would silently index from the
    end of the list in Python."""
    bad = good_landscape(funding_events=[FundingEvent(
        company_name="Co", round=FundingStage.SEED, date="2026-01-01",
        source_claim_index=-1,
    )])
    passed, feedback = validate_landscape_analysis(output(bad))
    assert passed is False
    assert "negative" in feedback


def test_empty_incumbents_and_new_entrants_are_allowed():
    """Not every sector has a real incumbent story, and padding one is worse
    than reporting none."""
    passed, _ = validate_landscape_analysis(
        output(good_landscape(incumbents=[], new_entrants=[]))
    )
    assert passed is True


# ---------------------------------------------------------------------------
# Judgment
# ---------------------------------------------------------------------------

def test_well_formed_judgment_passes():
    passed, result = validate_investment_judgment(output(good_judgment()))
    assert passed is True
    assert isinstance(result, TaskOutput)


def test_empty_recommendations_is_rejected():
    """A briefing that takes no position has failed at its purpose."""
    passed, feedback = validate_investment_judgment(output(good_judgment(recommendations=[])))
    assert passed is False
    assert "failed at its purpose" in feedback


def test_recommendation_citing_no_claims_is_rejected():
    bad = good_judgment(recommendations=[Recommendation(
        action=RecommendationAction.PASS, text="Pass on this sector.",
        supporting_claim_indices=[],
    )])
    passed, feedback = validate_investment_judgment(output(bad))
    assert passed is False
    assert "cites no supporting claims" in feedback


def test_empty_tensions_list_is_allowed():
    """Manufacturing disagreement to look rigorous is its own dishonesty."""
    passed, _ = validate_investment_judgment(output(good_judgment(tensions_or_conflicts=[])))
    assert passed is True


def test_one_line_executive_summary_is_rejected():
    passed, feedback = validate_investment_judgment(
        output(good_judgment(executive_summary="Looks good."))
    )
    assert passed is False
    assert "executive_summary" in feedback


def test_wall_of_text_executive_summary_is_rejected():
    passed, feedback = validate_investment_judgment(
        output(good_judgment(executive_summary=" ".join(f"Sentence {i}." for i in range(12))))
    )
    assert passed is False
    assert "house style is 3-5" in feedback


def test_summary_slightly_over_the_house_style_is_still_allowed():
    """The band is wider than the style guide on purpose - a deterministic gate
    catches egregious violations; the Report Crew's style reviewer handles
    polish. Six sentences must not fail the pipeline."""
    six = " ".join(f"Sentence number {i} here." for i in range(6))
    passed, _ = validate_investment_judgment(output(good_judgment(executive_summary=six)))
    assert passed is True


# ---------------------------------------------------------------------------
# Sentence counting - the part most likely to misfire on real prose
# ---------------------------------------------------------------------------

def test_decimal_numbers_do_not_count_as_sentence_breaks():
    """Funding figures are everywhere in these summaries; '$4.5 billion' must
    not read as two sentences."""
    assert _count_sentences("The sector drew $4.5 billion in 2026.") == 1


def test_multiple_sentences_are_counted():
    assert _count_sentences("First one. Second one! Third one?") == 3
