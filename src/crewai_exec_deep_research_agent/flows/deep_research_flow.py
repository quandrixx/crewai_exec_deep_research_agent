"""
Orchestrating Flow for the Deep Research Agent.

Shape: intake -> Research Crew (parallel internal+external) -> Analysis Crew
-> deterministic fact-check gate -> [pass: Report Crew] / [fail: bounded retry,
then human escalation].

The fact-check gate now verifies THREE things pulled from AnalysisResult,
not just recommendations - this changed once the models became domain-specific:
  1. Recommendation.supporting_claim_indices resolve to real claims
  2. CompanyProfile.supporting_claim_indices resolve to real claims
  3. FundingEvent.source_claim_index resolves to a real claim, since funding
     amounts/dates are exactly the kind of specific, checkable detail an
     investment committee would want traced back to a real source.
"""

from pydantic import BaseModel
from crewai.flow.flow import Flow, start, listen, router

from crewai_exec_deep_research_agent.models import (
    ResearchFindings,
    AnalysisResult,
    FactCheckResult,
    FinalReport,
)


class ResearchState(BaseModel):
    topic: str = ""
    research: ResearchFindings | None = None
    analysis: AnalysisResult | None = None
    fact_check: FactCheckResult | None = None
    final_report: FinalReport | None = None
    revision_count: int = 0


class DeepResearchFlow(Flow[ResearchState]):

    @start()
    def intake_topic(self):
        # self.state.topic populated from kickoff(inputs={"topic": "..."})
        print(f"Researching: {self.state.topic}")

    @listen(intake_topic)
    def run_research(self):
        # Kicks off the Research Crew, whose external and internal researchers
        # run concurrently. Use .run() rather than .crew().kickoff(): the crew
        # emits two separate ClaimList outputs plus a barrier task's
        # confirmation line, and .run() is what merges them into one
        # ResearchFindings.
        from ..crews.research_crew.research_crew import ResearchCrew
        self.state.research = ResearchCrew().run(self.state.topic)

    @listen(run_research)
    def run_analysis(self):
        # .run() rather than .crew().kickoff(): the crew emits two partial
        # objects (landscape, then judgment) and never emits all_claims at all,
        # so there is no single CrewOutput.pydantic to take. .run() merges the
        # halves and attaches the claim list the agents were shown, which is
        # what makes the fact-check gate's index lookups meaningful.
        from ..crews.analysis_crew.analysis_crew import AnalysisCrew
        self.state.analysis = AnalysisCrew().run(self.state.research)

    @listen(run_analysis)
    def fact_check(self):
        # Deterministic - not a crew. Checks recommendation, company, and
        # funding-event claim indices all resolve against all_claims.
        from ..tools.citation_check_tool import check_citations
        self.state.fact_check = check_citations(self.state.analysis)

    # Routing labels are deliberately NOT the names of the methods they
    # trigger. CrewAI rejects a flow whose listener is triggered by its own
    # handler name - it reads as a self-triggering infinite loop - so the
    # events are named for the decision and the handlers for the work.
    @router(fact_check)
    def route_by_fact_check(self):
        if self.state.fact_check.passed:
            return "ready_for_report"
        if self.state.revision_count < 1:
            self.state.revision_count += 1
            return "needs_revision"
        return "needs_human_review"

    @listen("needs_revision")
    def revise_analysis(self):
        # Re-run Analysis Crew, feeding the specific fact_check issues back
        # in as extra context so the retry has something concrete to fix
        # rather than just "try again."
        from ..crews.analysis_crew.analysis_crew import AnalysisCrew
        from ..tools.citation_check_tool import check_citations

        self.state.analysis = AnalysisCrew().run(
            self.state.research,
            prior_issues=self.state.fact_check.issues,
        )
        # Re-run the gate here rather than calling fact_check() directly:
        # a plain method call would update state without re-triggering the
        # router, so the revised analysis would never actually be re-judged.
        self.state.fact_check = check_citations(self.state.analysis)

    @router(revise_analysis)
    def route_after_revision(self):
        # Bounded at exactly one revision - the first router only sends work
        # here when revision_count was still 0, so there is no second retry to
        # consider. Either the fix worked or a human looks at it.
        if self.state.fact_check.passed:
            return "ready_for_report"
        return "needs_human_review"

    @listen("ready_for_report")
    def generate_report(self):
        # .run() rather than .crew().kickoff(): the sources appendix, the
        # funding table, and fact_check_status are assembled in Python, not by
        # an agent, so there is no single CrewOutput.pydantic to take.
        #
        # The status is read from the gate rather than hardcoded. Only a clean
        # first pass is "passed" - a report that needed a revision round is
        # marked passed_with_flags, which render_markdown() surfaces on the
        # face of the document. A reader should never have to know the
        # pipeline's history to know how much to trust the output.
        from ..crews.report_crew.report_crew import ReportCrew

        status = "passed" if self.state.revision_count == 0 else "passed_with_flags"
        self.state.final_report = ReportCrew().run(
            self.state.analysis,
            fact_check_status=status,
        )

    @listen("needs_human_review")
    def flag_for_human(self):
        # Terminal state - report withheld. Surface exactly which claims
        # failed so a human analyst has a head start rather than starting
        # the whole research topic over.
        print("Fact-check failed twice. Escalating for human review:")
        for issue in self.state.fact_check.issues:
            print(f"  - {issue.claim_or_entity}: {issue.problem}")