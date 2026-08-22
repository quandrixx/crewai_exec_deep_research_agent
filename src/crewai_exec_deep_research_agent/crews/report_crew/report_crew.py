"""
Report Crew loader - stage three of the Deep Research Flow.

Renders a fact-checked AnalysisResult into a FinalReport. Two sequential
tasks - write the briefing, then style-review it - plus the parts that should
never touch an LLM at all.

**What's deterministic here, and why each one.**

  - `_render_funding_table()`. models.py says it outright: FundingEvent is
    "kept structured so the Report Crew can render this as a deterministic
    markdown table instead of asking an LLM to format numbers correctly from
    prose." A misplaced decimal in a funding figure is the kind of error a
    partner catches and remembers.

  - `_build_sources_appendix()`. Built from AnalysisResult.all_claims, which
    is the same list the analysis cited by index and the fact-check gate
    verified against. A model writing its own Sources section would be writing
    URLs from memory - the one place a plausible-looking fabrication would slip
    past every check we have, because nothing downstream re-reads the sources.

  - `fact_check_status`. Comes from the Flow's citation gate, not from an
    agent's opinion of its own work.

What's left for the agents is prose, which is the only part they're better at
than Python.
"""

from pathlib import Path

from crewai import Crew
from crewai.crews.crew_output import CrewOutput
from crewai.project import load_crew

from crewai_exec_deep_research_agent.knowledge_paths import read_knowledge_file
from crewai_exec_deep_research_agent.costs import LEDGER
from crewai_exec_deep_research_agent.models import (
    AnalysisResult,
    FinalReport,
    FundingEvent,
    ReportDraft,
    ReviewedReport,
    SourceType,
    SourcedClaim,
)


_CREW_CONFIG = Path(__file__).parent / "crew.jsonc"

_FORMAT_TASK = "format_report_task"
_REVIEW_TASK = "style_review_task"

_STYLE_GUIDE = "style_guide.md"
_SAMPLE_REPORT = "prior_exec_report_sample.md"

# The style guide prefers a table over bullets once there are this many rounds.
_TABLE_THRESHOLD = 3


def _format_amount(amount: float | None) -> str:
    """Render a funding figure the way a partner expects to scan it."""
    if amount is None:
        return "Undisclosed"
    if amount >= 1_000_000_000:
        return f"${amount / 1_000_000_000:.2f}B".replace(".00B", "B")
    if amount >= 1_000_000:
        return f"${amount / 1_000_000:.0f}M"
    return f"${amount:,.0f}"


def _render_funding_table(events: list[FundingEvent]) -> str:
    """Render funding rounds as markdown - a table, or bullets if there are few.

    Columns follow the style guide exactly: Company, Round, Amount, Date,
    Lead Investor.
    """
    if not events:
        return (
            "(No specific funding rounds were found in the research. Say so "
            "plainly in this section - an absence of capital is itself a "
            "finding, not a gap to paper over.)"
        )

    if len(events) < _TABLE_THRESHOLD:
        return "\n".join(
            f"- **{event.company_name}** — {event.round.value.replace('_', ' ').title()}, "
            f"**{_format_amount(event.amount_usd)}** ({event.date})"
            + (f", led by **{event.lead_investor}**" if event.lead_investor else "")
            for event in events
        )

    rows = ["| Company | Round | Amount | Date | Lead Investor |",
            "| --- | --- | --- | --- | --- |"]
    for event in events:
        rows.append(
            f"| **{event.company_name}** "
            f"| {event.round.value.replace('_', ' ').title()} "
            f"| **{_format_amount(event.amount_usd)}** "
            f"| {event.date} "
            f"| {event.lead_investor or '—'} |"
        )
    return "\n".join(rows)


_CAPITAL_HEADING = "Where Capital Is Flowing"


def _insert_funding_block(body: str, funding_block: str) -> str:
    """Put the canonical funding figures into the report, replacing nothing.

    The writer is told not to produce a table at all, and the guardrail rejects
    one - so this inserts the rendered block directly after the 'Where Capital
    Is Flowing' heading, leaving the agent's interpretation of the figures
    underneath it.

    Asking the model to paste a pre-rendered table verbatim was tried first and
    does not hold. On a live run the model rewrote the table from the
    underlying claims instead, shipping 'EUR 32M' where the structured data
    said $36M and 'Premium to market' where it said $4M. Inserting the block in
    Python is the only version of this that is actually guaranteed.
    """
    lines = body.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("## ") and _CAPITAL_HEADING.lower() in line.lower():
            return "\n".join([*lines[: index + 1], "", funding_block, *lines[index + 1:]])

    # The guardrail requires this section, so reaching here means the section
    # was lost after validation. Append rather than silently drop the figures.
    return f"{body}\n\n## {_CAPITAL_HEADING}\n\n{funding_block}"


def _build_sources_appendix(claims: list[SourcedClaim]) -> list[str]:
    """Every distinct source behind the report, external first then internal.

    Deduplicated because one source often backs several claims, and ordered so
    the appendix reads consistently across briefings. Derived from the claim
    list rather than from the report text, so a source can only appear here if
    it actually backed a gathered claim.
    """
    external, internal = [], []
    for claim in claims:
        bucket = external if claim.source_type is SourceType.EXTERNAL else internal
        if claim.source not in bucket:
            bucket.append(claim.source)
    return [*sorted(external), *sorted(internal)]


def _format_analysis_block(analysis: AnalysisResult) -> str:
    """Render the analysis for the writer's prompt.

    Claim indices are deliberately left out. The fact-check gate has already
    verified them, the style guide keeps citations out of the body, and showing
    them invites the writer to sprinkle '[3]' through the prose.
    """
    lines = [f"TOPIC: {analysis.topic}", "", "EXECUTIVE SUMMARY (use essentially verbatim):",
             analysis.executive_summary, "", "WHAT'S CHANGING:"]
    lines += [f"  - {shift.description}" for shift in analysis.market_shifts]

    lines += ["", "INCUMBENTS:"]
    lines += [
        f"  - {c.name}: {c.differentiation}"
        + (f" (founded {c.founded_year})" if c.founded_year else "")
        for c in analysis.incumbents
    ] or ["  (none identified)"]

    lines += ["", "NEW ENTRANTS:"]
    lines += [
        f"  - {c.name} [{c.funding_stage.value}]: {c.differentiation}"
        + (f" (founded {c.founded_year})" if c.founded_year else "")
        for c in analysis.new_entrants
    ] or ["  (none identified)"]

    lines += ["", "WHERE SOURCES DISAGREE:"]
    lines += [f"  - {t.statement}" for t in analysis.tensions_or_conflicts] or [
        "  (none - internal and external sources agree; OMIT this section entirely)"
    ]

    lines += ["", "RECOMMENDATIONS:"]
    lines += [
        f"  - [{r.action.value}] {r.text}" for r in analysis.recommendations
    ]
    return "\n".join(lines)


class ReportCrew:
    """Renders a fact-checked analysis into the finished briefing."""

    def crew(self) -> Crew:
        """The configured Crew, for callers that want to drive kickoff directly."""
        crew, _default_inputs = load_crew(_CREW_CONFIG)
        return crew

    def run(
        self,
        analysis: AnalysisResult,
        fact_check_status: str = "passed",
    ) -> FinalReport:
        """Render `analysis` into a FinalReport.

        This is the entry point the Flow uses. Prefer it over crew().kickoff():
        the sources appendix, the funding table, and fact_check_status are all
        assembled here rather than by an agent.
        """
        crew = self.crew()
        result = crew.kickoff(inputs={
            "topic": analysis.topic,
            "style_guide": read_knowledge_file(_STYLE_GUIDE),
            "sample_report": read_knowledge_file(_SAMPLE_REPORT),
            "analysis_block": _format_analysis_block(analysis),
            "funding_table": _render_funding_table(analysis.funding_events),
        })
        LEDGER.record("report", crew, result)

        reviewed = _task_output(result, _REVIEW_TASK, ReviewedReport)

        body = _insert_funding_block(
            reviewed.body_markdown,
            _render_funding_table(analysis.funding_events),
        )

        return FinalReport(
            title=reviewed.title,
            # From the analysis, not the draft: the strategist wrote it, the
            # fact-check gate ran against it, and it should not drift during
            # formatting.
            executive_summary=analysis.executive_summary,
            body_markdown=body,
            sources_appendix=_build_sources_appendix(analysis.all_claims),
            fact_check_status=fact_check_status,
        )


def render_markdown(report: FinalReport) -> str:
    """Assemble the complete document, appendix included.

    The Sources section lives here rather than in body_markdown because the
    style guide wants it as an appendix, and because building it from the claim
    list is what guarantees every listed source actually backed a claim.
    """
    parts = [f"# {report.title}", "", report.body_markdown, "", "## Sources", ""]

    if report.sources_appendix:
        parts += [f"- {source}" for source in report.sources_appendix]
    else:
        parts.append("- (No sources were gathered for this briefing.)")

    if report.fact_check_status != "passed":
        parts += ["", f"> **Fact-check status: {report.fact_check_status}** — "
                      f"this briefing did not fully pass automated citation "
                      f"verification and needs human review before use."]
    return "\n".join(parts)


def _task_output(result: CrewOutput, task_name: str, expected_type: type):
    """Pull one task's parsed output by name, or fail loudly."""
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
        f"updating report_crew.py."
    )
