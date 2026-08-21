"""
Deterministic guardrails for the two Report Crew tasks.

The style guide is unusually checkable for a style guide - it names required
sections, an explicit order, a length target, and specific banned words - so a
lot of it can be enforced without an LLM. That's the split of labor here: this
module checks structure, and the Style Reviewer agent handles the parts that
genuinely need judgment (is this decisive, is this vague, does this read as
promotional).

Two rules are enforced strictly, because both are correctness rather than taste:

  - No '## Sources' section. The appendix is built in Python from the claim
    list (report_crew.py). A model writing its own Sources section would be
    writing URLs from memory, which is exactly how a fabricated citation gets
    into an otherwise well-cited report.
  - Required sections present and in order, since AnalysisResult's fields map
    onto them one-to-one and a missing section means content was dropped.

Length is enforced loosely. The style guide targets 800-1100 words but says
explicitly that a briefing needing more room to be honest should say so rather
than silently truncating, so the band here only catches a stub or a runaway.
"""

import re
from typing import Any

from crewai.tasks.task_output import TaskOutput

from crewai_exec_deep_research_agent.models import ReportDraft, ReviewedReport


# In the order the style guide requires them. 'Where Sources Disagree' is
# deliberately absent: it is conditional, included only when genuine tension
# exists, and the guide says to omit rather than pad.
_REQUIRED_SECTIONS = (
    "Executive Summary",
    "What's Changing",
    "Competitive Landscape",
    "Where Capital Is Flowing",
    "Investment Recommendation",
)

# Style guide section 2: "Partners read hype-adjacent language as a signal the
# underlying thesis is weak."
_BANNED_PHRASES = (
    "revolutionary",
    "game-changing",
    "game changing",
    "poised to disrupt",
    "paradigm shift",
)

# Style guide targets 800-1100 words in the body. The band below is wider on
# both sides, because the guide explicitly allows a briefing that needs more
# room to be honest - but not by much. An earlier, far looser ceiling (1600)
# let a live run ship well over target while the Style Reviewer reported
# nothing to fix, so the gate was doing no work at exactly the point the guide
# cares about most: partners stop reading.
_MIN_WORDS = 600
_MAX_WORDS = 1300


def _heading_positions(body: str) -> dict[str, int]:
    """Character offset of each '## Heading' found, keyed by heading text."""
    return {
        match.group(1).strip(): match.start()
        for match in re.finditer(r"^##\s+(.+?)\s*$", body, re.MULTILINE)
    }


def _find_section(headings: dict[str, int], wanted: str) -> int | None:
    """Locate a section by prefix, tolerating the guide's longer variants.

    'Competitive Landscape' must match the guide's full
    'Competitive Landscape: Leaders & New Entrants'.
    """
    for heading, position in headings.items():
        if heading.lower().startswith(wanted.lower()):
            return position
    return None


def _check_report_body(title: str, body: str, problems: list[str]) -> None:
    """Structural checks shared by both tasks."""
    if not title.strip():
        problems.append("title is empty")

    if not body.strip():
        problems.append("body_markdown is empty")
        return

    headings = _heading_positions(body)

    found: list[tuple[str, int]] = []
    for section in _REQUIRED_SECTIONS:
        position = _find_section(headings, section)
        if position is None:
            problems.append(
                f"missing required section '## {section}' - the report's "
                f"structure is fixed by the house style"
            )
        else:
            found.append((section, position))

    # Order only makes sense for sections that are actually present.
    ordered = [name for name, _ in sorted(found, key=lambda pair: pair[1])]
    expected = [name for name in _REQUIRED_SECTIONS if name in {n for n, _ in found}]
    if ordered != expected:
        problems.append(
            f"sections are out of order: found {ordered}, expected {expected}"
        )

    if any(line.lstrip().startswith("|") for line in body.splitlines()):
        problems.append(
            "body_markdown contains a markdown table. Do not write one - the "
            "funding figures are rendered from structured data and inserted "
            "automatically. Write only your interpretation of what the funding "
            "pattern means. (A model rewriting the table from the underlying "
            "claims once shipped 'EUR 32M' where the verified figure was $36M.)"
        )

    if _find_section(headings, "Sources") is not None:
        problems.append(
            "body_markdown contains a '## Sources' section. Remove it - the "
            "sources appendix is generated from the verified claim list and "
            "appended automatically, so writing one by hand risks citing a "
            "source that was never gathered"
        )

    word_count = len(body.split())
    if word_count < _MIN_WORDS:
        problems.append(
            f"body_markdown is only {word_count} words; the house style targets "
            f"800-1100 and this is too thin to be a briefing"
        )
    elif word_count > _MAX_WORDS:
        problems.append(
            f"body_markdown runs to {word_count} words against an 800-1100 target. "
            f"Cut roughly {word_count - 1100} words. Tighten prose and drop the "
            f"weakest supporting detail in each section - do not drop a whole "
            f"required section, and do not cut the risk statement from any "
            f"recommendation"
        )

    lowered = body.lower()
    for phrase in _BANNED_PHRASES:
        if phrase in lowered:
            problems.append(
                f"contains promotional language ('{phrase}') - partners read "
                f"hype-adjacent wording as a sign the thesis is weak"
            )


def _result(problems: list[str], output: TaskOutput) -> tuple[bool, Any]:
    if not problems:
        return (True, output)
    return (
        False,
        f"{len(problems)} style problem(s): " + "; ".join(problems[:6]) +
        ". Fix these against the house style guide and return the corrected report.",
    )


def validate_report_draft(output: TaskOutput) -> tuple[bool, Any]:
    """Guardrail for the formatting task."""
    draft = output.pydantic
    if not isinstance(draft, ReportDraft):
        return (
            False,
            "Output could not be parsed into the required ReportDraft schema. "
            "Return a JSON object with exactly two keys: title (string) and "
            "body_markdown (string). Return ONLY that object, with no "
            "surrounding prose.",
        )

    problems: list[str] = []
    _check_report_body(draft.title, draft.body_markdown, problems)
    return _result(problems, output)


def validate_reviewed_report(output: TaskOutput) -> tuple[bool, Any]:
    """Guardrail for the style review task.

    Same structural rules - a reviewer that 'fixed' the draft into an invalid
    shape is a worse outcome than one that changed nothing.
    """
    reviewed = output.pydantic
    if not isinstance(reviewed, ReviewedReport):
        return (
            False,
            "Output could not be parsed into the required ReviewedReport "
            "schema. Return a JSON object with exactly three keys: title "
            "(string), body_markdown (string), and revision_notes (list of "
            "strings, which may be empty). Return ONLY that object.",
        )

    problems: list[str] = []
    _check_report_body(reviewed.title, reviewed.body_markdown, problems)

    for index, note in enumerate(reviewed.revision_notes):
        if not note.strip():
            problems.append(f"revision_notes[{index}] is empty")

    return _result(problems, output)
