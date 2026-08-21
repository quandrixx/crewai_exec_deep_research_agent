"""
test_report_crew.py

Tests the Report Crew's deterministic half - the parts deliberately kept away
from an LLM because a mistake there is both easy to make and hard to spot:
the funding table, the sources appendix, and the final document assembly.

The sources appendix gets the most attention. It is the one place where a
fabrication would slip past every other check in the pipeline: the fact-check
gate verifies claim indices, not source strings, and nothing downstream
re-reads the appendix. Building it from the verified claim list is what makes
"every source listed actually backed a claim" true by construction rather than
by hope.
"""

import pytest  # pyrefly: ignore
from crewai.crews.crew_output import CrewOutput
from crewai.tasks.task_output import TaskOutput

from crewai_exec_deep_research_agent.crews.report_crew.report_crew import (
    ReportCrew,
    _build_sources_appendix,
    _format_amount,
    _format_analysis_block,
    _insert_funding_block,
    _render_funding_table,
    render_markdown,
)
from crewai_exec_deep_research_agent.models import (
    AnalysisResult,
    CompanyProfile,
    EntityType,
    FinalReport,
    FundingEvent,
    FundingStage,
    MarketShift,
    Recommendation,
    RecommendationAction,
    ReviewedReport,
    SourcedClaim,
    SourceType,
)


REVIEW_TASK = "style_review_task"

BODY = """## Executive Summary
It is investable. Timing favors entry. Regulatory risk dominates.

## What's Changing
- **Three** designs approved.

## Competitive Landscape: Leaders & New Entrants
- **Acme** leads.

## Where Capital Is Flowing
- **Acme** raised **$42M**.

## Investment Recommendation
1. Source deals, with regulatory risk.
"""


def claim(text: str, source: str, source_type: SourceType) -> SourcedClaim:
    return SourcedClaim(claim=text, source=source, source_type=source_type, confidence=0.9)


def analysis(**overrides) -> AnalysisResult:
    return AnalysisResult(**{
        "topic": "small modular reactors",
        "executive_summary": "It is investable. Timing favors entry. Risk is regulatory.",
        "market_shifts": [MarketShift(description="Approvals accelerated.",
                                      supporting_claim_indices=[0])],
        "incumbents": [CompanyProfile(name="Acme", entity_type=EntityType.INCUMBENT,
                                      differentiation="Only approved design.",
                                      supporting_claim_indices=[0])],
        "new_entrants": [],
        "funding_events": [],
        "tensions_or_conflicts": [],
        "recommendations": [Recommendation(action=RecommendationAction.SOURCE_DEALS,
                                           text="Source deals; regulatory risk.",
                                           supporting_claim_indices=[0])],
        "all_claims": [
            claim("Approvals accelerated.", "https://example.test/a", SourceType.EXTERNAL),
            claim("Firm passed before.", "internal_thesis.md", SourceType.INTERNAL),
        ],
        **overrides,
    })


def event(company="Acme", amount=42_000_000, stage=FundingStage.SERIES_A,
          date="2026-03-01", lead="Big Fund") -> FundingEvent:
    return FundingEvent(company_name=company, round=stage, amount_usd=amount,
                        date=date, lead_investor=lead, source_claim_index=0)


class StubCrew:
    def __init__(self, output):
        self.output = output
        self.received_inputs = None

    def kickoff(self, inputs=None):
        self.received_inputs = inputs
        return self.output


def reviewed_output(title="Should We Invest?", body=BODY, notes=None) -> CrewOutput:
    return CrewOutput(
        raw="{}",
        tasks_output=[TaskOutput(
            name=REVIEW_TASK, description="d", raw="{}", agent="Style Reviewer",
            pydantic=ReviewedReport(title=title, body_markdown=body,
                                    revision_notes=notes or []),
        )],
        token_usage={},
    )


# ---------------------------------------------------------------------------
# Sources appendix
# ---------------------------------------------------------------------------

def test_sources_appendix_deduplicates_repeated_sources():
    """One source usually backs several claims; the appendix lists it once."""
    claims = [
        claim("A.", "https://example.test/one", SourceType.EXTERNAL),
        claim("B.", "https://example.test/one", SourceType.EXTERNAL),
        claim("C.", "https://example.test/two", SourceType.EXTERNAL),
    ]
    assert _build_sources_appendix(claims) == [
        "https://example.test/one", "https://example.test/two",
    ]


def test_sources_appendix_lists_external_before_internal():
    claims = [
        claim("A.", "internal_thesis.md", SourceType.INTERNAL),
        claim("B.", "https://example.test/x", SourceType.EXTERNAL),
    ]
    assert _build_sources_appendix(claims) == [
        "https://example.test/x", "internal_thesis.md",
    ]


def test_sources_appendix_contains_only_sources_that_backed_a_claim():
    """The invariant worth having. It holds by construction because the
    appendix is derived from all_claims - the same list the fact-check gate
    verified - rather than parsed back out of generated prose."""
    result = analysis()
    appendix = _build_sources_appendix(result.all_claims)
    assert set(appendix) == {c.source for c in result.all_claims}


def test_empty_claims_produce_an_empty_appendix():
    assert _build_sources_appendix([]) == []


# ---------------------------------------------------------------------------
# Funding table
# ---------------------------------------------------------------------------

def test_three_or_more_events_render_as_a_table():
    """The style guide prefers a table once there are 3+ rounds."""
    table = _render_funding_table([event(), event("Beta"), event("Gamma")])
    assert table.startswith("| Company | Round | Amount | Date | Lead Investor |")
    assert table.count("\n") == 4  # header, separator, three rows


def test_two_events_render_as_bullets():
    rendered = _render_funding_table([event(), event("Beta")])
    assert rendered.startswith("- **Acme**")
    assert "|" not in rendered


def test_no_funding_events_says_so_rather_than_rendering_an_empty_table():
    """An absence of capital is a finding, per the style guide - not a gap to
    quietly paper over with an empty table."""
    rendered = _render_funding_table([])
    assert "No specific funding rounds" in rendered
    assert "finding" in rendered


def test_undisclosed_amounts_are_labelled_not_zeroed():
    rendered = _render_funding_table([event(amount=None), event("B", amount=None),
                                      event("C", amount=None)])
    assert "Undisclosed" in rendered
    assert "$0" not in rendered


def test_missing_lead_investor_renders_a_dash():
    rendered = _render_funding_table([event(lead=None), event("B"), event("C")])
    assert "| — |" in rendered


@pytest.mark.parametrize("amount,expected", [
    (1_000_000_000, "$1B"),
    (1_500_000_000, "$1.50B"),
    (700_000_000, "$700M"),
    (42_000_000, "$42M"),
    (None, "Undisclosed"),
])
def test_amounts_are_formatted_for_scanning(amount, expected):
    assert _format_amount(amount) == expected


# ---------------------------------------------------------------------------
# Funding block insertion - the determinism guarantee
# ---------------------------------------------------------------------------

def test_funding_block_is_inserted_under_its_heading():
    body = _insert_funding_block(BODY, "| Company | Round |\n| --- | --- |")
    capital = body.index("## Where Capital Is Flowing")
    table = body.index("| Company | Round |")
    next_section = body.index("## Investment Recommendation")
    assert capital < table < next_section


def test_inserted_figures_are_the_structured_ones_not_the_models(monkeypatch):
    """The reason this is inserted rather than pasted by the writer. On a live
    run the model rewrote the table from the underlying claims, shipping
    'EUR 32M' where the verified figure was $36M and 'Premium to market' where
    it was $4M. Inserting in Python is what actually guarantees the numbers."""
    events = [event("Panthalassa", 140_000_000), event("CorPower", 35_840_000),
              event("Eco Wave", 4_000_000)]
    stub = StubCrew(reviewed_output())
    monkeypatch.setattr(ReportCrew, "crew", lambda self: stub)

    report = ReportCrew().run(analysis(funding_events=events))

    assert _render_funding_table(events) in report.body_markdown
    assert "**$36M**" in report.body_markdown
    assert "EUR" not in report.body_markdown


def test_funding_block_is_appended_if_the_heading_somehow_vanished():
    """The guardrail requires the section, so this is a belt-and-braces path -
    but silently dropping verified funding figures would be the worst possible
    failure mode here."""
    body = _insert_funding_block("## Executive Summary\nText.", "TABLE-HERE")
    assert "TABLE-HERE" in body
    assert "## Where Capital Is Flowing" in body


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------

def test_analysis_block_tells_the_writer_to_omit_an_empty_tensions_section():
    """Padding a 'Where Sources Disagree' section with manufactured tension is
    a specific failure the style guide calls out."""
    block = _format_analysis_block(analysis(tensions_or_conflicts=[]))
    assert "OMIT this section entirely" in block


def test_analysis_block_includes_tensions_when_they_exist():
    block = _format_analysis_block(
        analysis(tensions_or_conflicts=["Internal thesis contradicts recent evidence."])
    )
    assert "Internal thesis contradicts recent evidence." in block
    assert "OMIT this section" not in block


def test_analysis_block_omits_claim_indices():
    """Citations belong in the appendix, not sprinkled through the prose - and
    showing indices to the writer invites exactly that."""
    block = _format_analysis_block(analysis())
    assert "[0]" not in block


# ---------------------------------------------------------------------------
# Assembly of the final report
# ---------------------------------------------------------------------------

def test_run_assembles_the_report_from_reviewer_output_and_python_parts(monkeypatch):
    stub = StubCrew(reviewed_output())
    monkeypatch.setattr(ReportCrew, "crew", lambda self: stub)

    source = analysis()
    report = ReportCrew().run(source)

    assert report.title == "Should We Invest?"
    # The reviewer's prose survives intact, with the verified funding block
    # inserted under its heading - the body is no longer a pure pass-through.
    assert "It is investable. Timing favors entry." in report.body_markdown
    assert "1. Source deals, with regulatory risk." in report.body_markdown
    assert _render_funding_table(source.funding_events) in report.body_markdown
    # Executive summary comes from the analysis, not the draft - it already
    # went through the strategist and the fact-check gate.
    assert report.executive_summary == source.executive_summary
    assert report.sources_appendix == _build_sources_appendix(source.all_claims)
    assert report.fact_check_status == "passed"


def test_run_passes_the_style_guide_and_funding_table_into_the_prompt(monkeypatch):
    stub = StubCrew(reviewed_output())
    monkeypatch.setattr(ReportCrew, "crew", lambda self: stub)

    ReportCrew().run(analysis(funding_events=[event(), event("B"), event("C")]))

    inputs = stub.received_inputs
    assert "Northbridge" in inputs["style_guide"]
    assert inputs["sample_report"].strip()
    assert inputs["funding_table"].startswith("| Company |")


def test_missing_review_output_raises_rather_than_shipping_the_unreviewed_draft(monkeypatch):
    monkeypatch.setattr(ReportCrew, "crew", lambda self: StubCrew(
        CrewOutput(raw="{}", tasks_output=[], token_usage={})
    ))
    with pytest.raises(RuntimeError, match="produced no output"):
        ReportCrew().run(analysis())


# ---------------------------------------------------------------------------
# Final document rendering
# ---------------------------------------------------------------------------

def test_rendered_markdown_appends_the_sources_section():
    report = FinalReport(
        title="Should We Invest?", executive_summary="Yes.", body_markdown=BODY,
        sources_appendix=["https://example.test/a", "internal_thesis.md"],
        fact_check_status="passed",
    )
    rendered = render_markdown(report)

    assert rendered.startswith("# Should We Invest?")
    assert "## Sources" in rendered
    assert "- https://example.test/a" in rendered
    # Sources come after the body, as an appendix.
    assert rendered.index("## Sources") > rendered.index("## Investment Recommendation")


def test_rendered_markdown_flags_a_report_that_failed_fact_checking():
    """A report that reaches a reader despite failing verification must say so
    on its face - the status field alone is invisible once it's a document."""
    report = FinalReport(
        title="T", executive_summary="S", body_markdown=BODY,
        sources_appendix=[], fact_check_status="passed_with_flags",
    )
    rendered = render_markdown(report)
    assert "passed_with_flags" in rendered
    assert "human review" in rendered


def test_clean_report_carries_no_fact_check_warning():
    report = FinalReport(
        title="T", executive_summary="S", body_markdown=BODY,
        sources_appendix=["https://example.test/a"], fact_check_status="passed",
    )
    assert "human review" not in render_markdown(report)
