"""
test_citation_check.py

The most valuable test file in this project, per the build spec - this is
pure Python testing pure Python, no LLM calls involved, so it should be
fast, deterministic, and exhaustive about edge cases. Each test name states
exactly which failure mode it's proving the gate catches.
"""

import pytest #pyrefly: ignore
from crewai_exec_deep_research_agent.models import (
    AnalysisResult,
    SourcedClaim,
    SourceType,
    CompanyProfile,
    EntityType,
    FundingStage,
    FundingEvent,
    Recommendation,
    RecommendationAction,
)
from crewai_exec_deep_research_agent.tools.citation_check_tool import check_citations


# ---------------------------------------------------------------------------
# Helpers - build minimal valid pieces so each test only varies the one
# thing it's actually testing.
# ---------------------------------------------------------------------------

def make_claim(text: str, source: str = "Test Source", source_type=SourceType.EXTERNAL) -> SourcedClaim:
    return SourcedClaim(claim=text, source=source, source_type=source_type, confidence=0.9)


def base_claims() -> list[SourcedClaim]:
    return [
        make_claim("Company A raised a $42M Series A round this year."),          # index 0
        make_claim("Non-PGM catalyst costs fell 35-40% since 2022."),             # index 1
        make_claim("Two governments introduced electrolyzer manufacturing tax credits."),  # index 2
    ]


def base_analysis(**overrides) -> AnalysisResult:
    defaults = dict(
        topic="Green hydrogen electrolyzers",
        executive_summary="Test summary.",
        market_shifts=[],
        incumbents=[],
        new_entrants=[],
        funding_events=[],
        tensions_or_conflicts=[],
        recommendations=[],
        all_claims=base_claims(),
    )
    defaults.update(overrides)
    return AnalysisResult(**defaults)


# ---------------------------------------------------------------------------
# Passing cases
# ---------------------------------------------------------------------------

def test_empty_analysis_passes_with_zero_verified():
    """No recommendations, companies, or funding events at all should pass
    trivially - an empty report has nothing to fact-check, and that's not
    itself a failure."""
    analysis = base_analysis()
    result = check_citations(analysis)
    assert result.passed is True
    assert result.verified_count == 0
    assert result.issues == []


def test_valid_recommendation_citation_passes():
    analysis = base_analysis(
        recommendations=[
            Recommendation(
                action=RecommendationAction.SOURCE_DEALS,
                text="Actively source Series A deals in non-PGM catalyst startups.",
                supporting_claim_indices=[1],  # shares "catalyst" wording
            )
        ]
    )
    result = check_citations(analysis)
    assert result.passed is True
    assert result.verified_count == 1


def test_valid_company_profile_citation_passes():
    analysis = base_analysis(
        new_entrants=[
            CompanyProfile(
                name="Company A",
                entity_type=EntityType.NEW_ENTRANT,
                funding_stage=FundingStage.SERIES_A,
                differentiation="Raised a Series A this year on catalyst work.",
                supporting_claim_indices=[0],
            )
        ]
    )
    result = check_citations(analysis)
    assert result.passed is True
    assert result.verified_count == 1


def test_valid_funding_event_citation_passes():
    analysis = base_analysis(
        funding_events=[
            FundingEvent(
                company_name="Company A",
                round=FundingStage.SERIES_A,
                amount_usd=42_000_000,
                date="this year",
                lead_investor="Growth-stage climate fund",
                source_claim_index=0,
            )
        ]
    )
    result = check_citations(analysis)
    assert result.passed is True
    assert result.verified_count == 1


# ---------------------------------------------------------------------------
# Structural failures - out-of-range or missing indices
# ---------------------------------------------------------------------------

def test_recommendation_with_out_of_range_index_fails():
    analysis = base_analysis(
        recommendations=[
            Recommendation(
                action=RecommendationAction.MONITOR,
                text="Monitor this sub-segment for two quarters.",
                supporting_claim_indices=[99],  # only 3 claims exist (0-2)
            )
        ]
    )
    result = check_citations(analysis)
    assert result.passed is False
    assert any("does not exist" in issue.problem for issue in result.issues)


def test_recommendation_with_no_citations_fails():
    analysis = base_analysis(
        recommendations=[
            Recommendation(
                action=RecommendationAction.PASS,
                text="Pass on this sub-segment for now.",
                supporting_claim_indices=[],
            )
        ]
    )
    result = check_citations(analysis)
    assert result.passed is False
    assert any("no supporting claims" in issue.problem for issue in result.issues)


def test_company_profile_with_out_of_range_index_fails():
    analysis = base_analysis(
        incumbents=[
            CompanyProfile(
                name="Incumbent Manufacturer",
                entity_type=EntityType.INCUMBENT,
                differentiation="Partnered with a materials-science startup on catalysts.",
                supporting_claim_indices=[5],  # out of range
            )
        ]
    )
    result = check_citations(analysis)
    assert result.passed is False
    assert any("Incumbent Manufacturer" in issue.claim_or_entity or
               "does not exist" in issue.problem for issue in result.issues)


def test_funding_event_with_out_of_range_index_fails():
    analysis = base_analysis(
        funding_events=[
            FundingEvent(
                company_name="Company B",
                round=FundingStage.SEED,
                amount_usd=11_000_000,
                date="last year",
                lead_investor="Deep-tech seed fund",
                source_claim_index=42,  # out of range
            )
        ]
    )
    result = check_citations(analysis)
    assert result.passed is False
    assert any("does not exist" in issue.problem for issue in result.issues)


def test_negative_index_is_also_caught():
    """Out-of-range isn't just 'too high' - a negative index should fail too,
    since Python's list[-1] would silently return the last element instead
    of erroring, which would let a bad citation slip through undetected."""
    analysis = base_analysis(
        recommendations=[
            Recommendation(
                action=RecommendationAction.MONITOR,
                text="Monitor this sub-segment.",
                supporting_claim_indices=[-1],
            )
        ]
    )
    result = check_citations(analysis)
    assert result.passed is False


# ---------------------------------------------------------------------------
# Weak-support heuristic - index is valid, but the citation looks spurious
# ---------------------------------------------------------------------------

def test_weak_support_citation_is_flagged():
    """The index exists, but the recommendation text shares essentially no
    vocabulary with the claim it points to - this is the case where an
    agent cited *something*, just not something that actually backs the claim."""
    analysis = base_analysis(
        recommendations=[
            Recommendation(
                action=RecommendationAction.MONITOR,
                text="Watch quarterly earnings calls for sentiment shifts.",
                supporting_claim_indices=[2],  # claim is about tax credits - no overlap
            )
        ]
    )
    result = check_citations(analysis)
    assert result.passed is False
    assert any("shared terminology" in issue.problem for issue in result.issues)


def test_paraphrased_but_related_citation_is_not_flagged_as_weak():
    """A citation that shares even one significant word with its claim
    should NOT trip the weak-support heuristic - it's deliberately
    conservative to avoid punishing reasonable paraphrasing."""
    analysis = base_analysis(
        recommendations=[
            Recommendation(
                action=RecommendationAction.PRIORITIZE_DILIGENCE,
                text="Prioritize diligence given falling catalyst costs.",
                supporting_claim_indices=[1],  # shares "catalyst"
            )
        ]
    )
    result = check_citations(analysis)
    assert result.passed is True


def test_citation_set_is_judged_as_a_whole_not_claim_by_claim():
    """Regression test for an escalation on genuinely good output.

    An entity normally cites several claims that each back a different part of
    it. Here the recommendation is about catalyst costs and cites two claims:
    one about catalysts, one about a funding round. Judging each claim
    separately flags the funding claim and fails the report - which is exactly
    what happened on a live end-to-end run, where a CorPower Ocean profile
    describing cost reductions cited a perfectly valid claim about its Series B
    and sent the whole briefing to human review.
    """
    analysis = base_analysis(
        recommendations=[
            Recommendation(
                action=RecommendationAction.PRIORITIZE_DILIGENCE,
                text="Prioritize diligence given falling catalyst costs.",
                # index 1 shares 'catalyst'; index 0 shares nothing with the text.
                supporting_claim_indices=[0, 1],
            )
        ]
    )
    result = check_citations(analysis)
    assert result.passed is True, [i.problem for i in result.issues]


def test_citation_set_with_nothing_relevant_is_still_flagged():
    """The relaxation above must not switch the heuristic off. A set where NO
    cited claim relates to the citing text is still the egregious case."""
    analysis = base_analysis(
        recommendations=[
            Recommendation(
                action=RecommendationAction.MONITOR,
                text="Watch quarterly earnings calls for sentiment shifts.",
                supporting_claim_indices=[0, 2],  # funding and tax credits - neither relates
            )
        ]
    )
    result = check_citations(analysis)
    assert result.passed is False
    assert any("shared terminology" in issue.problem for issue in result.issues)


def test_company_profile_is_supported_by_a_claim_naming_the_company():
    """A claim that names the company supports its profile even when the
    differentiation prose is about some other attribute - which is why the
    company's name is part of the text being matched."""
    analysis = base_analysis(
        all_claims=[make_claim("CorPower Ocean completed a Series B financing round.")],
        new_entrants=[
            CompanyProfile(
                name="CorPower Ocean",
                entity_type=EntityType.NEW_ENTRANT,
                differentiation="Published analysis showing 20% lower installed capacity.",
                supporting_claim_indices=[0],
            )
        ],
    )
    result = check_citations(analysis)
    assert result.passed is True, [i.problem for i in result.issues]


# ---------------------------------------------------------------------------
# Multiple simultaneous issues
# ---------------------------------------------------------------------------

def test_multiple_issues_are_all_reported_not_just_the_first():
    analysis = base_analysis(
        recommendations=[
            Recommendation(
                action=RecommendationAction.MONITOR,
                text="Bad recommendation one.",
                supporting_claim_indices=[99],
            ),
            Recommendation(
                action=RecommendationAction.PASS,
                text="Bad recommendation two.",
                supporting_claim_indices=[],
            ),
        ],
        funding_events=[
            FundingEvent(
                company_name="Company C",
                round=FundingStage.SERIES_A,
                amount_usd=28_000_000,
                date="this year",
                lead_investor="Strategic investor",
                source_claim_index=100,
            )
        ],
    )
    result = check_citations(analysis)
    assert result.passed is False
    assert len(result.issues) >= 3