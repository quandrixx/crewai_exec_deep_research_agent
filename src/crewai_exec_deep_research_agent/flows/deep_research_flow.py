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
        from ..crews.analysis_crew.analysis_crew import AnalysisCrew
        result = AnalysisCrew().crew().kickoff(inputs={
            "topic": self.state.topic,
            "internal_claims": self.state.research.internal_claims,
            "external_claims": self.state.research.external_claims,
        })
        self.state.analysis = result.pydantic  # -> AnalysisResult

    @listen(run_analysis)
    def fact_check(self):
        # Deterministic - not a crew. Checks recommendation, company, and
        # funding-event claim indices all resolve against all_claims.
        from ..tools.citation_check_tool import check_citations
        self.state.fact_check = check_citations(self.state.analysis)

    @router(fact_check)
    def route_by_fact_check(self):
        if self.state.fact_check.passed:
            return "generate_report"
        if self.state.revision_count < 1:
            self.state.revision_count += 1
            return "revise_analysis"
        return "flag_for_human"

    @listen("revise_analysis")
    def revise_analysis(self):
        # Re-run Analysis Crew, feeding the specific fact_check issues back
        # in as extra context so the retry has something concrete to fix
        # rather than just "try again."
        from ..crews.analysis_crew.analysis_crew import AnalysisCrew
        result = AnalysisCrew().crew().kickoff(inputs={
            "topic": self.state.topic,
            "internal_claims": self.state.research.internal_claims,
            "external_claims": self.state.research.external_claims,
            "prior_issues": [i.model_dump() for i in self.state.fact_check.issues],
        })
        self.state.analysis = result.pydantic
        return self.fact_check()  # loop back through the gate once

    @listen("generate_report")
    def generate_report(self):
        from ..crews.report_crew.report_crew import ReportCrew
        result = ReportCrew().crew().kickoff(inputs={
            "analysis": self.state.analysis,
            "fact_check_status": "passed",
        })
        self.state.final_report = result.pydantic  # -> FinalReport

    @listen("flag_for_human")
    def flag_for_human(self):
        # Terminal state - report withheld. Surface exactly which claims
        # failed so a human analyst has a head start rather than starting
        # the whole research topic over.
        print("Fact-check failed twice. Escalating for human review:")
        for issue in self.state.fact_check.issues:
            print(f"  - {issue.claim_or_entity}: {issue.problem}")