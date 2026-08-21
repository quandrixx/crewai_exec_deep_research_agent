"""
test_research_guardrails.py

Tests the Research Crew's deterministic output guardrails. These run between an
agent finishing and its output being accepted, so they're the last cheap chance
to catch a fabricated source before it reaches the Analysis Crew and starts
looking authoritative.

The behaviors that matter most here are the two easiest to get wrong in the
other direction:
  - an empty claim list must PASS, because "the firm has no prior work on this"
    is a real finding and rejecting it would apply exactly the pressure to
    fabricate that this project is built to avoid
  - a fabricated internal filename must FAIL, checked against the documents
    that actually exist rather than against a plausible-looking pattern

Guardrails are exercised through real TaskOutput objects rather than mocks,
since the contract with CrewAI is specifically "reads output.pydantic".
"""

import pytest  # pyrefly: ignore
from crewai.tasks.task_output import TaskOutput
from pydantic import ValidationError

from crewai_exec_deep_research_agent.crews.research_crew.research_guardrails import (
    validate_external_claims,
    validate_internal_claims,
)
from crewai_exec_deep_research_agent.models import ClaimList, SourcedClaim, SourceType
from crewai_exec_deep_research_agent.tools.internal_kb_tool import known_document_names


def make_output(claims: list[SourcedClaim] | None, raw: str = "{}") -> TaskOutput:
    """A TaskOutput shaped the way CrewAI hands one to a guardrail."""
    return TaskOutput(
        description="test task",
        raw=raw,
        agent="Test Agent",
        pydantic=ClaimList(claims=claims) if claims is not None else None,
    )


def a_real_internal_document() -> str:
    """Pick a genuine filename so tests don't hardcode one that gets renamed."""
    docs = sorted(known_document_names())
    assert docs, "internal docs must be loadable for these tests to mean anything"
    return docs[0]


# ---------------------------------------------------------------------------
# The empty-list case - deliberately valid
# ---------------------------------------------------------------------------

def test_empty_claim_list_passes_internal():
    """Finding nothing internally is a legitimate result, not a failure."""
    passed, _ = validate_internal_claims(make_output([]))
    assert passed is True


def test_empty_claim_list_fails_external():
    """The asymmetry is deliberate. The public web always has something to say
    about a real sector, so an empty external result means the research broke -
    which is exactly what happened in a live run, where a truncated response
    silently became zero claims."""
    passed, feedback = validate_external_claims(make_output([]))
    assert passed is False
    assert "not a plausible outcome" in feedback


def test_claim_list_rejects_missing_claims_key():
    """ClaimList.claims must stay REQUIRED. If it ever regains a default, a
    truncated agent response validates into an empty list instead of failing to
    parse, and real findings vanish without a trace."""
    with pytest.raises(ValidationError):
        ClaimList.model_validate({})


# ---------------------------------------------------------------------------
# Unparseable output
# ---------------------------------------------------------------------------

def test_unparsed_output_fails_with_schema_instructions():
    """When pydantic parsing failed entirely, the feedback has to restate the
    schema - the agent's retry is otherwise a coin flip."""
    passed, feedback = validate_external_claims(make_output(None, raw="not json at all"))
    assert passed is False
    assert "ClaimList" in feedback
    assert "confidence" in feedback


# ---------------------------------------------------------------------------
# External claims
# ---------------------------------------------------------------------------

def test_valid_external_claims_pass():
    claims = [
        SourcedClaim(
            claim="Company A raised $42M in a Series A round in March 2026.",
            source="https://example-news.test/company-a-series-a",
            source_type=SourceType.EXTERNAL,
            confidence=0.9,
        ),
    ]
    passed, result = validate_external_claims(make_output(claims))
    assert passed is True
    # On success the guardrail passes the output through untouched.
    assert isinstance(result, TaskOutput)


def test_external_claim_citing_a_publication_name_is_rejected():
    """'TechCrunch' isn't traceable; the style guide requires checkable sources."""
    claims = [
        SourcedClaim(
            claim="A molten salt startup raised a large round.",
            source="TechCrunch",
            source_type=SourceType.EXTERNAL,
            confidence=0.7,
        ),
    ]
    passed, feedback = validate_external_claims(make_output(claims))
    assert passed is False
    assert "not a URL" in feedback


def test_external_claim_tagged_internal_is_rejected():
    """Mislabelling would corrupt the internal/external split the report's
    'Where Sources Disagree' section depends on."""
    claims = [
        SourcedClaim(
            claim="Something about the sector.",
            source="https://example-news.test/story",
            source_type=SourceType.INTERNAL,
            confidence=0.8,
        ),
    ]
    passed, feedback = validate_external_claims(make_output(claims))
    assert passed is False
    assert "source_type" in feedback


def test_external_claim_with_empty_text_is_rejected():
    claims = [
        SourcedClaim(
            claim="   ",
            source="https://example-news.test/story",
            source_type=SourceType.EXTERNAL,
            confidence=0.8,
        ),
    ]
    passed, feedback = validate_external_claims(make_output(claims))
    assert passed is False
    assert "empty claim text" in feedback


# ---------------------------------------------------------------------------
# Internal claims
# ---------------------------------------------------------------------------

def test_valid_internal_claim_citing_a_real_document_passes():
    claims = [
        SourcedClaim(
            claim="Northbridge previously passed on this sector on timeline grounds.",
            source=a_real_internal_document(),
            source_type=SourceType.INTERNAL,
            confidence=0.9,
        ),
    ]
    passed, _ = validate_internal_claims(make_output(claims))
    assert passed is True


def test_internal_claim_citing_a_fabricated_document_is_rejected():
    """The highest-value check here: a plausible-looking but invented memo
    filename would otherwise reach the report's Sources appendix."""
    claims = [
        SourcedClaim(
            claim="The firm concluded this sector was too early.",
            source="internal_thesis_that_does_not_exist.md",
            source_type=SourceType.INTERNAL,
            confidence=0.9,
        ),
    ]
    passed, feedback = validate_internal_claims(make_output(claims))
    assert passed is False
    assert "not a real internal document" in feedback
    # Feedback names the documents that DO exist, so the retry can self-correct.
    assert a_real_internal_document() in feedback


def test_internal_claim_citing_a_url_is_rejected():
    """External knowledge restated as an internal firm view is the specific
    failure mode the internal researcher's prompt warns against."""
    claims = [
        SourcedClaim(
            claim="Industry analysts expect costs to fall.",
            source="https://example-news.test/analysis",
            source_type=SourceType.INTERNAL,
            confidence=0.6,
        ),
    ]
    passed, feedback = validate_internal_claims(make_output(claims))
    assert passed is False
    assert "not a real internal document" in feedback


# ---------------------------------------------------------------------------
# Feedback quality - the retry depends on it
# ---------------------------------------------------------------------------

def test_feedback_truncates_long_problem_lists_but_reports_the_total():
    """A retry prompt listing 30 individual failures is unreadable; the agent
    still needs to know the real scale of the problem."""
    claims = [
        SourcedClaim(
            claim=f"Claim number {i}.",
            source="not-a-url",
            source_type=SourceType.EXTERNAL,
            confidence=0.5,
        )
        for i in range(9)
    ]
    passed, feedback = validate_external_claims(make_output(claims))
    assert passed is False
    assert "9 claim(s) failed validation" in feedback
    assert "and 4 more claim(s)" in feedback


def test_feedback_tells_the_agent_to_drop_rather_than_relabel():
    """Without this, the cheapest way for an agent to satisfy the guardrail is
    to invent a source that matches the required shape."""
    claims = [
        SourcedClaim(
            claim="Unsourceable assertion.",
            source="somewhere",
            source_type=SourceType.EXTERNAL,
            confidence=0.5,
        ),
    ]
    _, feedback = validate_external_claims(make_output(claims))
    assert "Drop any claim you cannot properly source" in feedback
