"""
Analysis Crew loader - stage two of the Deep Research Flow.

Turns ResearchFindings into an AnalysisResult. Two sequential tasks: the Sector
Analyst organizes the evidence, then the Investment Strategist reads that and
commits to a position. This module merges the two outputs.

**The claim-index contract, which is the important thing in this file.**
Everything downstream - the fact-check gate, the report's traceability, the
Sources appendix - depends on `AnalysisResult.all_claims` being the exact list
the agents were shown, in the exact order they were shown it. So:

  - `_build_claim_index()` defines the ordering ONCE (external, then internal)
  - `_format_claims()` renders that same list, numbered, into the prompt
  - the merge puts that same list into `all_claims`

The agents never reproduce the claims themselves. Asking an LLM to echo 28
claims verbatim would be pure cost and risk: reworded claim text would break
every index reference, and a large payload is exactly what got truncated in a
live Research Crew run. The agents emit only indices; Python supplies the list.

If you change the ordering, change it in `_build_claim_index()` alone -
everything else derives from it.
"""

from pathlib import Path

from crewai import Crew
from crewai.crews.crew_output import CrewOutput
from crewai.project import load_crew

from crewai_exec_deep_research_agent.costs import LEDGER
from crewai_exec_deep_research_agent.models import (
    AnalysisResult,
    CitationIssue,
    InvestmentJudgment,
    LandscapeAnalysis,
    ResearchFindings,
    SourcedClaim,
)


_CREW_CONFIG = Path(__file__).parent / "crew.jsonc"

_LANDSCAPE_TASK = "landscape_analysis_task"
_JUDGMENT_TASK = "investment_judgment_task"


def _build_claim_index(findings: ResearchFindings) -> list[SourcedClaim]:
    """The single definition of claim ordering. External first, then internal.

    Any change here silently renumbers every citation in the report, so it must
    stay the only place the order is decided.
    """
    return [*findings.external_claims, *findings.internal_claims]


def _format_claims(claims: list[SourcedClaim]) -> str:
    """Render the claim list for the prompt, numbered by position.

    The index shown is the list position, which is what the agents cite and
    what citation_check_tool later resolves against all_claims.
    """
    if not claims:
        return "(No claims were gathered. Do not invent findings.)"

    lines = []
    for index, claim in enumerate(claims):
        lines.append(
            f"[{index}] ({claim.source_type.value}, confidence {claim.confidence}) "
            f"{claim.claim} [source: {claim.source}]"
        )
    return "\n".join(lines)


def _format_prior_issues(issues: list[CitationIssue] | None) -> str:
    """Render fact-check failures for a retry pass.

    Empty on a first attempt. On a revision the Flow passes the specific
    citation problems, so the retry has something concrete to fix rather than
    just being told to try again.
    """
    if not issues:
        return ""

    lines = [
        "A PREVIOUS ATTEMPT FAILED THE CITATION CHECK. Fix these specific",
        "problems - every one is a claim reference that could not be verified:",
    ]
    for issue in issues:
        lines.append(f"  - {issue.claim_or_entity}: {issue.problem}")
    lines.append(
        "Re-check each index you cite against the numbered list above. Drop any "
        "entry you cannot trace to a real claim rather than renumbering it to "
        "something that happens to exist."
    )
    return "\n".join(lines)


class AnalysisCrew:
    """Turns gathered claims into a structured, traceable investment analysis."""

    def crew(self) -> Crew:
        """The configured Crew, for callers that want to drive kickoff directly."""
        crew, _default_inputs = load_crew(_CREW_CONFIG)
        return crew

    def run(
        self,
        findings: ResearchFindings,
        prior_issues: list[CitationIssue] | None = None,
    ) -> AnalysisResult:
        """Analyze `findings` and return the merged, citable result.

        This is the entry point the Flow uses. Prefer it over crew().kickoff():
        the crew emits two partial objects and never emits all_claims at all,
        so CrewOutput.pydantic alone cannot give you an AnalysisResult.
        """
        all_claims = _build_claim_index(findings)

        crew = self.crew()
        result = crew.kickoff(inputs={
            "topic": findings.topic,
            "claims_block": _format_claims(all_claims),
            "prior_issues_block": _format_prior_issues(prior_issues),
        })
        LEDGER.record("analysis", crew, result)

        landscape = _task_output(result, _LANDSCAPE_TASK, LandscapeAnalysis)
        judgment = _task_output(result, _JUDGMENT_TASK, InvestmentJudgment)

        return AnalysisResult(
            topic=findings.topic,
            executive_summary=judgment.executive_summary,
            market_shifts=landscape.market_shifts,
            incumbents=landscape.incumbents,
            new_entrants=landscape.new_entrants,
            funding_events=landscape.funding_events,
            tensions_or_conflicts=judgment.tensions_or_conflicts,
            recommendations=judgment.recommendations,
            # Supplied here, never by an agent - see the module docstring.
            all_claims=all_claims,
        )


def _task_output(result: CrewOutput, task_name: str, expected_type: type):
    """Pull one task's parsed output by name, or fail loudly.

    By name rather than index so that adding a task later cannot silently
    start returning the wrong half of the analysis.
    """
    for task_output in result.tasks_output:
        if task_output.name != task_name:
            continue
        parsed = task_output.pydantic
        if not isinstance(parsed, expected_type):
            raise RuntimeError(
                f"Task '{task_name}' did not produce a valid "
                f"{expected_type.__name__} (got {type(parsed).__name__}). Its "
                f"raw output was: {(task_output.raw or '')[:500]}"
            )
        return parsed

    found = [task_output.name for task_output in result.tasks_output]
    raise RuntimeError(
        f"Task '{task_name}' produced no output. Tasks that did run: {found}. "
        f"This usually means a task name in crew.jsonc was changed without "
        f"updating analysis_crew.py."
    )
