"""
test_citation_check.py

The most valuable test file in this project, per the build spec - this is
pure Python testing pure Python, no LLM calls involved, so it should be
fast, deterministic, and exhaustive about edge cases. Each test name states
exactly which failure mode it's proving the gate catches.
"""

import pathlib

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
from crewai_exec_deep_research_agent.tools.citation_check_tool import (
    check_citations,
    _distinctive_claim_terms,
    _distinctive_name_tokens,
    _overlap_count,
    _significant_words,
)


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


# A realistically-sized single-sector corpus. The name check filters name
# tokens by how often they appear across the run's own claims, so a 3-claim
# fixture cannot exercise it - with 16 claims, a token is distinctive when it
# appears in fewer than 4 of them. Here 'power', 'wave' and 'marine' are
# common enough to be sector vocabulary; 'corpower', 'minesto', 'eco',
# 'orbital' and 'bp' are not.
def sector_claims() -> list[SourcedClaim]:
    return [
        make_claim("CorPower Ocean closed a $36M Series B led by Northern Capital."),   # 0
        make_claim("CorPower Ocean reported 20% lower installed cost per megawatt."),   # 1
        make_claim("Minesto commissioned its Dragon 12 kite at the Faroe site."),       # 2
        make_claim("Minesto raised a bridge round in early 2025."),                     # 3
        make_claim("Eco Wave Power signed a grid connection agreement in Portugal."),   # 4
        make_claim("Tocardo suspended tidal turbine deliveries pending recert."),       # 5
        make_claim("The Orbital O2 turbine at EMEC ran for 18 continuous months."),     # 6
        make_claim("BP wrote down its stake in a floating tidal developer."),           # 7
        make_claim("Global wave power funding reached $310M across 14 rounds."),        # 8
        make_claim("Two governments introduced marine power production tax credits."),  # 9
        make_claim("Grid operators flagged queues as a wave power bottleneck."),        # 10
        make_claim("Installed tidal power capacity in Europe grew 18% year on year."),  # 11
        make_claim("Wave power levelized cost estimates fell 12% since 2023."),         # 12
        make_claim("Marine power insurers raised premiums on early deployments."),      # 13
        make_claim("Utility procurement for wave power is concentrated in Europe."),    # 14
        make_claim("Marine power supply chains depend on a few cable vendors."),        # 15
    ]


def sector_analysis(**overrides) -> AnalysisResult:
    overrides.setdefault("all_claims", sector_claims())
    return base_analysis(**overrides)


def profile(name: str, indices: list[int], differentiation: str) -> CompanyProfile:
    return CompanyProfile(
        name=name,
        entity_type=EntityType.NEW_ENTRANT,
        differentiation=differentiation,
        supporting_claim_indices=indices,
    )


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
# A company profile must cite at least one claim that names the company
# ---------------------------------------------------------------------------

def test_profile_citing_only_another_companys_claims_is_flagged():
    """The gap this check exists to close, in the case weak support cannot see.

    The profile is attributed to CorPower Ocean but its differentiation is
    lifted from Minesto's evidence, so it shares three distinctive terms
    ('dragon', 'kite', 'faroe') with the cited claim - comfortably enough to
    satisfy weak support, which only ever asks whether the text and the claim
    are about the same thing. They are. They are just not about the same
    COMPANY, and only the naming check can tell.
    """
    analysis = sector_analysis(
        new_entrants=[profile(
            "CorPower Ocean", [2],
            "Runs a Dragon-class kite tuned for Faroe sea conditions.",
        )],
    )
    result = check_citations(analysis)
    assert result.passed is False
    assert len(result.issues) == 1, [i.problem for i in result.issues]
    assert "mention the company by name" in result.issues[0].problem


def test_one_claim_naming_the_company_carries_the_whole_citation_set():
    """Set-level, matching the weak-support aggregation: the other cited
    claims support other parts of the profile and need not name it."""
    analysis = sector_analysis(
        new_entrants=[profile(
            "CorPower Ocean", [0, 10],
            "Backs its cost claims against grid interconnection constraints.",
        )],
    )
    result = check_citations(analysis)
    assert result.passed is True, [i.problem for i in result.issues]


def test_sector_words_in_a_company_name_do_not_count_as_naming_it():
    """'Wave' and 'Power' are what this whole run is about. If they counted,
    the check would pass on any claim in the corpus and do no work."""
    analysis = sector_analysis(
        new_entrants=[profile(
            "Eco Wave Power", [10, 12],
            "Onshore wave energy conversion mounted to existing structures.",
        )],
    )
    result = check_citations(analysis)
    assert result.passed is False
    assert any("mention the company by name" in i.problem for i in result.issues)


def test_a_two_letter_company_name_is_still_matched():
    """_significant_words drops tokens of three characters or fewer, which is
    right for prose overlap and would erase 'BP' entirely - hence the separate
    raw tokenizer."""
    assert _significant_words("BP") == set()
    analysis = sector_analysis(
        incumbents=[profile(
            "BP", [7],
            "Wrote down its floating tidal stake after a strategy reset.",
        )],
    )
    result = check_citations(analysis)
    assert result.passed is True, [i.problem for i in result.issues]


def test_a_claim_using_part_of_the_company_name_counts_as_naming_it():
    """Claims say 'the Orbital O2', not the full registered company name.
    Requiring every name token would flag correct output - as it did on the
    saved wave and tidal run."""
    analysis = sector_analysis(
        incumbents=[profile(
            "Orbital Marine Power", [6],
            "Floating tidal platform with the longest continuous run to date.",
        )],
    )
    result = check_citations(analysis)
    assert result.passed is True, [i.problem for i in result.issues]


def test_check_is_skipped_when_no_part_of_the_name_is_distinctive():
    """A name made entirely of sector vocabulary cannot be tested for. The
    conservative choice is to say nothing: a false positive here escalates a
    correct briefing to a human, which is the expensive failure."""
    assert _distinctive_name_tokens("Wave Power", [c.claim for c in sector_claims()]) == set()
    analysis = sector_analysis(
        new_entrants=[profile(
            "Wave Power", [2, 3],
            "Raised a bridge round ahead of a commercial kite deployment.",
        )],
    )
    result = check_citations(analysis)
    assert result.passed is True, [i.problem for i in result.issues]


def test_every_saved_run_still_passes_the_gate():
    """Guards the over-strict direction against real output rather than
    invented fixtures. All 26 company profiles across the five saved runs
    cite at least one claim naming them; if a future tightening breaks that,
    it breaks here instead of on a live run."""
    output_dir = pathlib.Path(__file__).resolve().parent.parent / "output"
    saved = sorted(output_dir.glob("*/analysis.json"))
    assert saved, "no saved runs found to check against"
    for path in saved:
        analysis = AnalysisResult.model_validate_json(path.read_text())
        result = check_citations(analysis)
        assert result.passed is True, (
            f"{path.parent.name}: {[i.problem for i in result.issues]}"
        )


# ---------------------------------------------------------------------------
# Weak support counts only terms that are distinctive within the run
# ---------------------------------------------------------------------------

def test_sector_vocabulary_alone_no_longer_counts_as_support():
    """The core of the df weighting. Every claim in a run is about one sector,
    so 'wave' and 'power' are shared by most of the corpus and evidence
    nothing. This citation used to pass on those two words alone."""
    claim = sector_claims()[10].claim
    text = "Source deals in wave power generation at utility scale."
    assert _overlap_count(text, claim) >= 1, "precondition: it does share words"

    analysis = sector_analysis(
        recommendations=[Recommendation(
            action=RecommendationAction.SOURCE_DEALS,
            text=text,
            supporting_claim_indices=[10],
        )],
    )
    result = check_citations(analysis)
    assert result.passed is False
    assert "shared terminology" in result.issues[0].problem


def test_one_distinctive_term_is_not_enough_when_the_text_offers_several():
    """'minesto', 'dragon' and 'kite' are all distinctive here, so the bar is
    two. The bridge-round claim shares only the company name; the kite claim
    shares all three."""
    def recommend(index: int) -> AnalysisResult:
        return sector_analysis(recommendations=[Recommendation(
            action=RecommendationAction.PRIORITIZE_DILIGENCE,
            text="Prioritize diligence on Minesto given the Dragon kite results.",
            supporting_claim_indices=[index],
        )])

    assert check_citations(recommend(3)).passed is False
    assert check_citations(recommend(2)).passed is True


def test_an_entity_with_only_one_distinctive_term_is_held_to_one():
    """The bar scales to the signal available. A funding event's citing text is
    structured fields - a stage enum and a raw float that no claim ever spells
    that way - so 'minesto' is all it really has. Holding it to two flagged
    correct events on the saved runs (X-energy, Fervo)."""
    analysis = sector_analysis(
        funding_events=[FundingEvent(
            company_name="Minesto",
            round=FundingStage.UNKNOWN,
            amount_usd=1020000000.0,
            date="2025-01",
            source_claim_index=3,
        )],
    )
    result = check_citations(analysis)
    assert result.passed is True, [i.problem for i in result.issues]


def test_funding_event_pointing_at_another_companys_claim_is_flagged():
    """Funding events get the naming check too - the same reasoning as company
    profiles, and the only real check they have."""
    analysis = sector_analysis(
        funding_events=[FundingEvent(
            company_name="Tocardo",
            round=FundingStage.UNKNOWN,
            date="2025-01",
            source_claim_index=3,  # a Minesto claim
        )],
    )
    result = check_citations(analysis)
    assert result.passed is False
    assert any("mention the company by name" in i.problem for i in result.issues)


def test_a_corpus_too_small_to_rank_terms_falls_back_to_plain_overlap():
    """With three claims no term can clear the frequency bar, so there is
    nothing to weight by. Falling back to the unfiltered check keeps the gate
    lenient rather than flagging everything - a false positive escalates a
    correct briefing to a human."""
    assert _distinctive_claim_terms([c.claim for c in base_claims()]) == set()
    analysis = base_analysis(recommendations=[Recommendation(
        action=RecommendationAction.SOURCE_DEALS,
        text="Actively source Series A deals in non-PGM catalyst startups.",
        supporting_claim_indices=[1],
    )])
    assert check_citations(analysis).passed is True


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