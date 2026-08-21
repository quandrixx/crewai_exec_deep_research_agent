"""
test_flow_wiring.py

Tests that the orchestrating Flow is structurally valid and routes correctly.

Worth having because CrewAI validates a Flow's shape at construction time, and
nothing else in the suite constructs one. The flow skeleton shipped with three
handlers listening to labels identical to their own method names, which CrewAI
rejects as self-triggering - and that failure was invisible until every crew
existed and someone actually instantiated the flow.

No LLM calls here: routing decisions are pure functions of ResearchState, so
they can be driven directly.
"""

import pytest  # pyrefly: ignore

from crewai_exec_deep_research_agent.flows.deep_research_flow import (
    DeepResearchFlow,
    ResearchState,
)
from crewai_exec_deep_research_agent.models import CitationIssue, FactCheckResult


def flow_with(passed: bool, revision_count: int = 0) -> DeepResearchFlow:
    flow = DeepResearchFlow()
    flow.state.fact_check = FactCheckResult(
        passed=passed,
        verified_count=3,
        issues=[] if passed else [
            CitationIssue(claim_or_entity="A recommendation", problem="cites claim 99"),
        ],
    )
    flow.state.revision_count = revision_count
    return flow


def test_flow_constructs():
    """CrewAI validates flow structure at construction. This is the guard
    against a listener label colliding with a handler name."""
    assert DeepResearchFlow() is not None


def test_state_defaults_are_empty():
    state = ResearchState()
    assert state.topic == ""
    assert state.research is None
    assert state.revision_count == 0


# ---------------------------------------------------------------------------
# Routing after the fact-check gate
# ---------------------------------------------------------------------------

def test_clean_fact_check_goes_straight_to_the_report():
    assert flow_with(passed=True).route_by_fact_check() == "ready_for_report"


def test_first_failure_triggers_a_revision():
    flow = flow_with(passed=False, revision_count=0)
    assert flow.route_by_fact_check() == "needs_revision"
    # The counter must advance, or the retry bound means nothing.
    assert flow.state.revision_count == 1


def test_second_failure_escalates_instead_of_looping():
    """The retry is bounded at one. Without this the flow would re-analyze
    forever on a topic whose evidence genuinely cannot support the claims."""
    flow = flow_with(passed=False, revision_count=1)
    assert flow.route_by_fact_check() == "needs_human_review"
    assert flow.state.revision_count == 1


def test_revision_that_fixed_the_problem_proceeds_to_the_report():
    assert flow_with(passed=True, revision_count=1).route_after_revision() == "ready_for_report"


def test_revision_that_did_not_fix_the_problem_escalates():
    assert flow_with(passed=False, revision_count=1).route_after_revision() == "needs_human_review"


# ---------------------------------------------------------------------------
# Report status reflects the pipeline's actual history
# ---------------------------------------------------------------------------

def test_report_is_flagged_when_it_took_a_revision_round(monkeypatch):
    """A reader should never have to know the pipeline's history to know how
    much to trust the output, so a report that needed fixing says so."""
    captured = {}

    class StubReportCrew:
        def run(self, analysis, fact_check_status="passed"):
            captured["status"] = fact_check_status
            return "report"

    import crewai_exec_deep_research_agent.crews.report_crew.report_crew as mod
    monkeypatch.setattr(mod, "ReportCrew", StubReportCrew)

    flow = flow_with(passed=True, revision_count=1)
    flow.generate_report()
    assert captured["status"] == "passed_with_flags"

    flow = flow_with(passed=True, revision_count=0)
    flow.generate_report()
    assert captured["status"] == "passed"


def test_escalation_reports_every_failed_citation(capsys):
    """The escalation path withholds the report, so its only output is the
    head start it gives a human analyst."""
    flow = flow_with(passed=False, revision_count=1)
    flow.flag_for_human()

    printed = capsys.readouterr().out
    assert "A recommendation" in printed
    assert "cites claim 99" in printed
