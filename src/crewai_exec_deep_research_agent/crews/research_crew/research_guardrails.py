"""
Deterministic guardrails for the two Research Crew tasks.

Same instinct as citation_check_tool.py: the thing checking whether research
output is trustworthy shouldn't be another LLM. These are plain Python
predicates over the already-parsed ClaimList, run by CrewAI between the agent
finishing and the output being accepted. On failure the returned string is fed
back to the agent as retry feedback, so every message says specifically what
was wrong and what to do instead - "invalid output" would just produce another
coin flip.

What's checked, and why each one is worth catching here rather than later:

  - source_type matches the task that produced it. The external researcher
    tagging a claim "internal" would silently corrupt the internal/external
    split that the whole report's "Where Sources Disagree" section depends on.
  - internal sources name a document that actually exists. Checked against
    internal_kb_tool.known_document_names(), so an invented memo filename is
    caught immediately instead of reaching the Sources appendix looking
    authoritative.
  - external sources are real URLs. A bare "TechCrunch" or "industry reports"
    isn't traceable, and the style guide requires named, checkable sources.
  - claim text isn't empty or a placeholder.

Deliberately NOT checked: an empty claims list. "We found nothing" is a real
research result - the internal knowledge base genuinely has no prior work on
most topics - and a guardrail that rejected it would be applying exactly the
pressure to fabricate that the rest of this project is built to avoid.

Note confidence bounds aren't checked here: SourcedClaim already constrains
confidence to [0, 1] with Field(ge=0, le=1), so an out-of-range value fails
Pydantic parsing before a guardrail ever sees it.
"""

from typing import Any

from crewai.tasks.task_output import TaskOutput

from crewai_exec_deep_research_agent.models import ClaimList, SourceType
from crewai_exec_deep_research_agent.tools.internal_kb_tool import known_document_names


# How many bad claims to name in the feedback before truncating. Enough for the
# agent to see the pattern, few enough that the retry prompt stays readable.
_MAX_REPORTED = 5


def _format_problems(problems: list[str]) -> str:
    shown = problems[:_MAX_REPORTED]
    remainder = len(problems) - len(shown)
    text = "; ".join(shown)
    if remainder > 0:
        text += f"; and {remainder} more claim(s) with similar problems"
    return text


def _validate_claims(
    output: TaskOutput,
    expected_type: SourceType,
    source_requirement: str,
    allow_empty: bool,
) -> tuple[bool, Any]:
    claims = output.pydantic

    if not isinstance(claims, ClaimList):
        return (
            False,
            "Output could not be parsed into the required ClaimList schema. "
            "Return a JSON object with a single key 'claims' holding a list of "
            "objects, each with exactly these fields: claim (string), source "
            "(string), source_type (string), confidence (number between 0 and 1). "
            "Return ONLY that JSON object, with no surrounding prose. If your "
            "previous answer was long, keep each claim to one sentence so the "
            "whole object fits in a single response.",
        )

    if not claims.claims and not allow_empty:
        return (
            False,
            "No claims were returned. An empty result is not a plausible outcome "
            "for public web research on a real sector - it means the searches "
            "never ran, returned only errors, or the findings were lost. Run the "
            "searches again and report what you find. If every search genuinely "
            "failed with an error message, say so explicitly in a single claim "
            "sourced to the failure rather than returning nothing.",
        )

    problems: list[str] = []
    known_docs = known_document_names() if expected_type is SourceType.INTERNAL else set()

    for index, claim in enumerate(claims.claims):
        label = f"claim {index + 1}"

        if not claim.claim.strip():
            problems.append(f"{label} has empty claim text")
            continue

        excerpt = claim.claim.strip()[:60]

        if claim.source_type is not expected_type:
            problems.append(
                f"{label} ('{excerpt}...') is tagged source_type "
                f"'{claim.source_type.value}' but this task produces only "
                f"'{expected_type.value}' claims"
            )

        source = claim.source.strip()
        if not source:
            problems.append(f"{label} ('{excerpt}...') has no source")
            continue

        if expected_type is SourceType.EXTERNAL:
            if not source.startswith(("http://", "https://")):
                problems.append(
                    f"{label} ('{excerpt}...') cites '{source}', which is not a "
                    f"URL - cite the exact link the web_search tool returned"
                )
        elif known_docs and source not in known_docs:
            problems.append(
                f"{label} ('{excerpt}...') cites '{source}', which is not a real "
                f"internal document"
            )

    if problems:
        return (
            False,
            f"{len(problems)} claim(s) failed validation: {_format_problems(problems)}. "
            f"{source_requirement} Drop any claim you cannot properly source rather "
            f"than adjusting its source to pass this check.",
        )

    return (True, output)


def validate_external_claims(output: TaskOutput) -> tuple[bool, Any]:
    """Guardrail for the external/web research task."""
    return _validate_claims(
        output,
        SourceType.EXTERNAL,
        source_requirement=(
            "Every external claim must carry source_type 'external' and the exact "
            "URL returned by the web_search tool as its source."
        ),
        # Unlike the internal knowledge base, the public web always has
        # something to say about a real sector. An empty external result means
        # the research failed, not that the sector is undocumented.
        allow_empty=False,
    )


def validate_internal_claims(output: TaskOutput) -> tuple[bool, Any]:
    """Guardrail for the internal knowledge base research task."""
    known = sorted(known_document_names())
    available = ", ".join(known) if known else "(none currently loaded)"
    return _validate_claims(
        output,
        SourceType.INTERNAL,
        source_requirement=(
            f"Every internal claim must carry source_type 'internal' and, as its "
            f"source, one of the exact filenames the internal_kb_lookup tool "
            f"returned. Available documents: {available}."
        ),
        # Empty IS a legitimate internal result: Northbridge has prior work on a
        # handful of sectors and none at all on most. Rejecting it here would
        # apply exactly the pressure to fabricate an internal view that the
        # agent's own instructions warn against.
        allow_empty=True,
    )
