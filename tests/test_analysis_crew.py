"""
test_analysis_crew.py

Tests the Analysis Crew's deterministic parts: the claim-index contract, the
merge, and the prompt-formatting helpers.

The claim-index contract is the thing worth guarding hardest. Every citation in
the finished report is an integer offset into AnalysisResult.all_claims, and
the agents choose those integers by reading a numbered list rendered from the
same source. If the rendered numbering and all_claims ever disagree, every
recommendation in the report silently points at the wrong evidence - the
fact-check gate would still pass, because the indices all resolve. They would
just resolve to the wrong claims.
"""

import pytest  # pyrefly: ignore
from crewai.crews.crew_output import CrewOutput
from crewai.tasks.task_output import TaskOutput

from crewai_exec_deep_research_agent.crews.analysis_crew.analysis_crew import (
    AnalysisCrew,
    _build_claim_index,
    _format_claims,
    _format_prior_issues,
)
from crewai_exec_deep_research_agent.models import (
    CitationIssue,
    CompanyProfile,
    EntityType,
    FundingEvent,
    FundingStage,
    InvestmentJudgment,
    LandscapeAnalysis,
    MarketShift,
    Recommendation,
    RecommendationAction,
    ResearchFindings,
    SourcedClaim,
    SourceType,
)


LANDSCAPE_TASK = "landscape_analysis_task"
JUDGMENT_TASK = "investment_judgment_task"


def claim(text: str, source_type: SourceType, source: str = "s") -> SourcedClaim:
    return SourcedClaim(claim=text, source=source, source_type=source_type, confidence=0.9)


def findings(external: int = 2, internal: int = 2) -> ResearchFindings:
    return ResearchFindings(
        topic="test topic",
        external_claims=[
            claim(f"External {i}", SourceType.EXTERNAL, f"https://example.test/{i}")
            for i in range(external)
        ],
        internal_claims=[
            claim(f"Internal {i}", SourceType.INTERNAL, f"internal_doc_{i}.md")
            for i in range(internal)
        ],
    )


def landscape(**overrides) -> LandscapeAnalysis:
    return LandscapeAnalysis(**{
        "market_shifts": [MarketShift(description="A shift.", supporting_claim_indices=[0])],
        "incumbents": [CompanyProfile(
            name="Incumbent Co", entity_type=EntityType.INCUMBENT,
            differentiation="Does a specific distinct thing.", supporting_claim_indices=[0],
        )],
        "new_entrants": [],
        "funding_events": [FundingEvent(
            company_name="Incumbent Co", round=FundingStage.SERIES_A,
            amount_usd=42_000_000, date="2026-03-01", source_claim_index=1,
        )],
        **overrides,
    })


def judgment(**overrides) -> InvestmentJudgment:
    return InvestmentJudgment(**{
        "executive_summary": "The sector is investable. Timing favors early entry. Risk is regulatory.",
        "tensions_or_conflicts": [],
        "recommendations": [Recommendation(
            action=RecommendationAction.SOURCE_DEALS,
            text="Source deals now, with regulatory timelines as the main risk.",
            supporting_claim_indices=[0, 2],
        )],
        **overrides,
    })


def crew_output(landscape_out=None, judgment_out=None) -> CrewOutput:
    outputs = []
    if landscape_out is not None:
        outputs.append(TaskOutput(name=LANDSCAPE_TASK, description="d", raw="{}",
                                  agent="Sector Analyst", pydantic=landscape_out))
    if judgment_out is not None:
        outputs.append(TaskOutput(name=JUDGMENT_TASK, description="d", raw="{}",
                                  agent="Investment Strategist", pydantic=judgment_out))
    return CrewOutput(raw="{}", tasks_output=outputs, token_usage={})


class StubCrew:
    def __init__(self, output):
        self.output = output
        self.received_inputs = None

    def kickoff(self, inputs=None):
        self.received_inputs = inputs
        return self.output


# ---------------------------------------------------------------------------
# The claim-index contract
# ---------------------------------------------------------------------------

def test_claim_index_is_external_then_internal():
    index = _build_claim_index(findings(external=2, internal=2))
    assert [c.claim for c in index] == ["External 0", "External 1", "Internal 0", "Internal 1"]


def test_rendered_numbering_matches_all_claims_positions(monkeypatch):
    """The single most important invariant in this crew: the number the agent
    reads is the position in all_claims. If these ever drift apart, every
    citation in the report resolves to the wrong claim while still passing the
    fact-check gate."""
    stub = StubCrew(crew_output(landscape(), judgment()))
    monkeypatch.setattr(AnalysisCrew, "crew", lambda self: stub)

    result = AnalysisCrew().run(findings(external=3, internal=2))

    rendered = stub.received_inputs["claims_block"].split("\n")
    assert len(rendered) == len(result.all_claims)
    for position, claim_obj in enumerate(result.all_claims):
        assert rendered[position].startswith(f"[{position}] ")
        # The claim's own text must appear on its own numbered line.
        assert claim_obj.claim in rendered[position]


def test_all_claims_is_supplied_by_python_not_the_agent(monkeypatch):
    """The agents never emit claims - they only cite indices. all_claims must
    come through byte-identical to what the Research Crew gathered."""
    source = findings(external=2, internal=1)
    stub = StubCrew(crew_output(landscape(), judgment()))
    monkeypatch.setattr(AnalysisCrew, "crew", lambda self: stub)

    result = AnalysisCrew().run(source)

    assert result.all_claims == [*source.external_claims, *source.internal_claims]


def test_formatted_claims_carry_type_confidence_and_source():
    """The agent needs the source type to reason about internal/external
    tension, and the source so a partner can trace a figure."""
    rendered = _format_claims(_build_claim_index(findings(external=1, internal=1)))
    assert "[0] (external, confidence 0.9) External 0 [source: https://example.test/0]" in rendered
    assert "[1] (internal, confidence 0.9) Internal 0 [source: internal_doc_0.md]" in rendered


def test_empty_claim_list_says_so_rather_than_rendering_nothing():
    """An empty block would read as a missing variable; the agent must be told
    explicitly that there is nothing, and not to invent."""
    rendered = _format_claims([])
    assert "No claims" in rendered
    assert "Do not invent" in rendered


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

def test_merge_assembles_both_halves_into_one_analysis_result(monkeypatch):
    stub = StubCrew(crew_output(landscape(), judgment()))
    monkeypatch.setattr(AnalysisCrew, "crew", lambda self: stub)

    result = AnalysisCrew().run(findings())

    assert result.topic == "test topic"
    # from the landscape half
    assert [s.description for s in result.market_shifts] == ["A shift."]
    assert [c.name for c in result.incumbents] == ["Incumbent Co"]
    assert len(result.funding_events) == 1
    # from the judgment half
    assert result.executive_summary.startswith("The sector is investable.")
    assert len(result.recommendations) == 1


def test_merged_result_passes_the_flows_citation_gate(monkeypatch):
    """End-to-end check of the contract: a well-formed analysis whose indices
    were chosen against the rendered list must satisfy citation_check_tool,
    which resolves those same indices against all_claims.

    Uses realistic wording rather than the synthetic fixtures above, because
    the gate also applies a weak-support heuristic - a citation whose text
    shares no vocabulary at all with the claim it points at gets flagged. Real
    analysis output paraphrases its evidence, so it shares vocabulary."""
    from crewai_exec_deep_research_agent.tools.citation_check_tool import check_citations

    real_findings = ResearchFindings(
        topic="small modular reactors",
        external_claims=[
            claim("Regulatory approval timelines for new reactor designs shortened in 2026.",
                  SourceType.EXTERNAL, "https://example.test/regulatory"),
            claim("Incumbent Co raised a $42M Series A in March 2026.",
                  SourceType.EXTERNAL, "https://example.test/funding"),
        ],
        internal_claims=[
            claim("Northbridge previously passed on deals in this sector citing regulatory risk.",
                  SourceType.INTERNAL, "internal_thesis_advanced_nuclear.md"),
        ],
    )
    real_landscape = landscape(
        # Realistic wording here too: market shifts are weak-support checked
        # like everything else, and the default fixture's "A shift." shares no
        # vocabulary with any claim.
        market_shifts=[MarketShift(
            description="Regulatory approval timelines for reactor designs shortened in 2026.",
            supporting_claim_indices=[0],
        )],
        incumbents=[CompanyProfile(
            name="Incumbent Co", entity_type=EntityType.INCUMBENT,
            differentiation="Holds a shortened regulatory approval for its reactor design.",
            supporting_claim_indices=[0],
        )],
    )
    real_judgment = judgment(recommendations=[Recommendation(
        action=RecommendationAction.SOURCE_DEALS,
        text="Source deals now; regulatory timelines remain the main risk.",
        supporting_claim_indices=[0, 2],
    )])

    stub = StubCrew(crew_output(real_landscape, real_judgment))
    monkeypatch.setattr(AnalysisCrew, "crew", lambda self: stub)

    result = AnalysisCrew().run(real_findings)
    check = check_citations(result)

    assert check.passed, [i.problem for i in check.issues]


def test_missing_task_output_raises_rather_than_half_analyzing(monkeypatch):
    """A missing judgment half must not yield a report with no recommendations."""
    stub = StubCrew(crew_output(landscape(), None))
    monkeypatch.setattr(AnalysisCrew, "crew", lambda self: stub)

    with pytest.raises(RuntimeError, match="produced no output"):
        AnalysisCrew().run(findings())


def test_unparsed_task_output_raises(monkeypatch):
    broken = CrewOutput(
        raw="not json",
        tasks_output=[TaskOutput(name=LANDSCAPE_TASK, description="d", raw="not json",
                                 agent="Sector Analyst", pydantic=None)],
        token_usage={},
    )
    monkeypatch.setattr(AnalysisCrew, "crew", lambda self: StubCrew(broken))

    with pytest.raises(RuntimeError, match="did not produce a valid LandscapeAnalysis"):
        AnalysisCrew().run(findings())


# ---------------------------------------------------------------------------
# Schema coverage for real deep-tech funding
# ---------------------------------------------------------------------------

def test_funding_stage_covers_late_stage_and_non_venture_rounds():
    """Regression test for a failure that only appeared on real data.

    FundingStage originally stopped at series_b, and a live run on small
    modular reactors produced an X-energy Series D - which failed validation
    and took down the whole landscape task. Capital-intensive energy companies
    routinely raise late rounds, project debt, and go public, so the enum has
    to cover that or the crew breaks on exactly the sectors it was built for.
    """
    for stage in ("series_c", "series_d", "series_e", "growth", "debt", "public"):
        assert FundingStage(stage)

    # And the schema must accept one end-to-end, not just define the constant.
    event = FundingEvent(
        company_name="X-energy", round=FundingStage.SERIES_D,
        amount_usd=700_000_000, date="2025-11-30", source_claim_index=5,
    )
    assert event.round.value == "series_d"


# ---------------------------------------------------------------------------
# Retry feedback
# ---------------------------------------------------------------------------

def test_prior_issues_block_is_empty_on_a_first_pass():
    assert _format_prior_issues(None) == ""
    assert _format_prior_issues([]) == ""


def test_prior_issues_block_names_each_specific_failure():
    """The revision path's whole value is that the retry knows what broke."""
    block = _format_prior_issues([
        CitationIssue(claim_or_entity="Some recommendation", problem="cites claim index 99"),
    ])
    assert "Some recommendation" in block
    assert "cites claim index 99" in block
    # And it must not invite the cheapest fix - renumbering until something resolves.
    assert "Drop any" in block


def test_prior_issues_are_passed_into_the_crew(monkeypatch):
    stub = StubCrew(crew_output(landscape(), judgment()))
    monkeypatch.setattr(AnalysisCrew, "crew", lambda self: stub)

    AnalysisCrew().run(
        findings(),
        prior_issues=[CitationIssue(claim_or_entity="X", problem="bad index")],
    )

    assert "bad index" in stub.received_inputs["prior_issues_block"]
