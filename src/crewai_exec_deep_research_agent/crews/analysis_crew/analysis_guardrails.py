"""
Deterministic guardrails for the two Analysis Crew tasks.

Same instinct as the Research Crew's: plain Python between the agent finishing
and the output being accepted, with failure messages specific enough that the
retry has something concrete to fix.

**Scope note - what these deliberately do NOT check.** A guardrail receives only
the TaskOutput, never the task's inputs, so it cannot know how many claims were
actually available and therefore cannot tell whether claim index 47 exists.
That range check is the Flow's job: `tools/citation_check_tool.py` runs against
the assembled AnalysisResult and has all_claims in hand. What's checked here is
everything that IS decidable from the output alone - structure, non-emptiness,
and indices being plausible at all (non-negative integers). The two layers are
complementary: this one catches an agent that cited nothing, the Flow gate
catches an agent that cited something imaginary.

The style rules below (recommendation counts, executive summary length) are
enforced loosely on purpose. knowledge/style_guide.md asks for 3-5 sentences
and 2-4 recommendations; these bands are wider, because a deterministic gate
should catch a wall of text or a single throwaway line, not police prose. Same
tradeoff as citation_check_tool's conservative overlap threshold.
"""

import re
from typing import Any

from crewai.tasks.task_output import TaskOutput

from crewai_exec_deep_research_agent.models import InvestmentJudgment, LandscapeAnalysis


_MAX_REPORTED = 5

# Wider than the style guide's 3-5, for the reasons in the module docstring.
_MIN_SUMMARY_SENTENCES = 2
_MAX_SUMMARY_SENTENCES = 8

# Style guide asks for 2-4 recommendations; allow a little slack either side.
_MIN_RECOMMENDATIONS = 1
_MAX_RECOMMENDATIONS = 6


def _format_problems(problems: list[str]) -> str:
    shown = problems[:_MAX_REPORTED]
    remainder = len(problems) - len(shown)
    text = "; ".join(shown)
    if remainder > 0:
        text += f"; and {remainder} more with similar problems"
    return text


def _count_sentences(text: str) -> int:
    """Count sentence endings, without splitting on decimals.

    Requires whitespace or end-of-string after the terminator, so '$4.5
    billion' stays one sentence. Abbreviations like 'U.S. ' will still
    over-count, which is part of why the accepted band is wide.
    """
    return len([part for part in re.split(r"[.!?]+(?:\s|$)", text) if part.strip()])


def _check_indices(
    indices: list[int],
    label: str,
    problems: list[str],
    *,
    require_some: bool = True,
) -> None:
    """Everything about claim indices decidable without the claim list."""
    if require_some and not indices:
        problems.append(f"{label} cites no supporting claims")
        return
    for index in indices:
        if index < 0:
            problems.append(f"{label} cites claim index {index}, which is negative")


def _fail(problems: list[str], requirement: str) -> tuple[bool, Any]:
    return (
        False,
        f"{len(problems)} problem(s) found: {_format_problems(problems)}. {requirement}",
    )


def validate_landscape_analysis(output: TaskOutput) -> tuple[bool, Any]:
    """Guardrail for the landscape (evidence-organizing) task."""
    landscape = output.pydantic

    if not isinstance(landscape, LandscapeAnalysis):
        return (
            False,
            "Output could not be parsed into the required LandscapeAnalysis "
            "schema. Return a JSON object with exactly these keys: "
            "market_shifts, incumbents, new_entrants, funding_events. Return "
            "ONLY that JSON object, with no surrounding prose, and keep each "
            "text field to one or two sentences so the whole object fits in a "
            "single response.",
        )

    problems: list[str] = []

    if not landscape.market_shifts:
        problems.append(
            "market_shifts is empty - the report's 'What's Changing' section "
            "cannot be written without it"
        )
    for shift in landscape.market_shifts:
        label = f"Market shift '{shift.description[:50]}'"
        if not shift.description.strip():
            problems.append("A market shift has an empty description")
            continue
        _check_indices(shift.supporting_claim_indices, label, problems)

    for company in [*landscape.incumbents, *landscape.new_entrants]:
        label = f"Company '{company.name}'"
        if not company.name.strip():
            problems.append("A company profile has an empty name")
            continue
        if not company.differentiation.strip():
            problems.append(
                f"{label} has no differentiation - say what is actually "
                f"distinct about its approach, never just that it is active "
                f"in the space"
            )
        _check_indices(company.supporting_claim_indices, label, problems)

    for event in landscape.funding_events:
        label = f"Funding event for '{event.company_name}'"
        if not event.company_name.strip():
            problems.append("A funding event has an empty company_name")
            continue
        _check_indices([event.source_claim_index], label, problems)

    if problems:
        return _fail(
            problems,
            "Every entry must point at the numbered claims it came from, using "
            "the indices shown in the claim list. Drop any entry you cannot "
            "trace to a specific claim rather than inventing support for it.",
        )
    return (True, output)


def validate_investment_judgment(output: TaskOutput) -> tuple[bool, Any]:
    """Guardrail for the judgment (recommendations) task."""
    judgment = output.pydantic

    if not isinstance(judgment, InvestmentJudgment):
        return (
            False,
            "Output could not be parsed into the required InvestmentJudgment "
            "schema. Return a JSON object with exactly these keys: "
            "executive_summary, tensions_or_conflicts, recommendations. Return "
            "ONLY that JSON object, with no surrounding prose.",
        )

    problems: list[str] = []

    summary = judgment.executive_summary.strip()
    if not summary:
        problems.append("executive_summary is empty")
    else:
        sentences = _count_sentences(summary)
        if sentences < _MIN_SUMMARY_SENTENCES:
            problems.append(
                f"executive_summary is {sentences} sentence(s); it needs to "
                f"state the thesis, why now, and at what stage"
            )
        elif sentences > _MAX_SUMMARY_SENTENCES:
            problems.append(
                f"executive_summary runs to {sentences} sentences; the house "
                f"style is 3-5, since a partner should be able to read only "
                f"this and know whether to keep reading"
            )

    if not judgment.recommendations:
        problems.append(
            "recommendations is empty - a briefing that takes no position has "
            "failed at its purpose, even if the position is 'pass'"
        )
    elif len(judgment.recommendations) > _MAX_RECOMMENDATIONS:
        problems.append(
            f"{len(judgment.recommendations)} recommendations is too many to "
            f"vote on; the house style is 2-4"
        )

    for rec in judgment.recommendations:
        label = f"Recommendation '{rec.text[:50]}'"
        if not rec.text.strip():
            problems.append("A recommendation has empty text")
            continue
        _check_indices(rec.supporting_claim_indices, label, problems)

    for index, tension in enumerate(judgment.tensions_or_conflicts):
        label = f"tensions_or_conflicts[{index}]"
        if not tension.statement.strip():
            problems.append(f"{label} has an empty statement")
            continue
        # Both sides required. Whether the indices land on claims of the right
        # TYPE needs the claim list, so that half lives in the fact-check gate;
        # what is decidable from the output alone is decided here, where a
        # failure costs one task retry instead of a whole crew re-run.
        _check_indices(
            tension.internal_claim_indices, f"{label} internal side", problems,
        )
        _check_indices(
            tension.external_claim_indices, f"{label} external side", problems,
        )

    if problems:
        return _fail(
            problems,
            "Every recommendation must cite the numbered claims that justify "
            "it, so the committee can trace a decision back to evidence. An "
            "empty tensions_or_conflicts list is fine when internal and "
            "external sources genuinely agree - do not manufacture tension. "
            "Every tension you DO report must cite at least one internal and "
            "at least one external claim; that is what makes it a "
            "disagreement rather than an observation.",
        )
    return (True, output)
