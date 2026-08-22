"""
test_report_guardrails.py

Tests the Report Crew's deterministic style guardrails.

The house style guide is unusually checkable - named sections, a fixed order, a
length target, specific banned words - so a lot of it is enforced here rather
than by asking an LLM whether another LLM followed the rules. What's left for
the Style Reviewer agent is judgment: is this decisive, is this vague, does a
recommendation state its risk.

Two rules are correctness rather than taste and are enforced strictly: required
sections must be present and ordered, and the draft must not contain a
hand-written Sources section (the appendix is generated from the verified claim
list). Length allows a modest overrun but not a sprawl - the guide permits a
briefing that needs more room to be honest, while a runaway defeats the whole
purpose of a briefing. The house target is 800-1100 words; the gate accepts
600-1300. Word counts below are expressed against those bounds rather than
hardcoded, so a future retune moves one constant, not ten fixtures.
"""

import pytest  # pyrefly: ignore
from crewai.tasks.task_output import TaskOutput

from crewai_exec_deep_research_agent.crews.report_crew.report_guardrails import (
    _MAX_WORDS,
    validate_report_draft,
    validate_reviewed_report,
)
from crewai_exec_deep_research_agent.models import ReportDraft, ReviewedReport


SECTIONS = [
    "## Executive Summary",
    "## What's Changing",
    "## Competitive Landscape: Leaders & New Entrants",
    "## Where Capital Is Flowing",
    "## Investment Recommendation",
]


# Five required sections, so a body is roughly 5 * filler_words long. Sized
# off the guardrail's own band so retuning the band cannot silently invalidate
# these fixtures - the ceiling moved once already when it was given a reserve
# for the funding block the guardrail never sees.
_SECTION_COUNT = 5
IN_BAND = 180        # ~900 words: inside the 800-1100 house target
BELOW_BAND = 20      # ~100 words: a stub
ABOVE_BAND = (_MAX_WORDS // _SECTION_COUNT) + 60     # comfortably a runaway
JUST_UNDER_CEILING = (_MAX_WORDS // _SECTION_COUNT) - 10


def body(sections=None, filler_words=IN_BAND, extra="") -> str:
    """A structurally valid body, sized inside the accepted word band."""
    sections = SECTIONS if sections is None else sections
    filler = " ".join(["word"] * filler_words)
    return "\n\n".join(f"{heading}\n{filler}" for heading in sections) + extra


def draft_output(title="Should Northbridge Invest in X?", text=None) -> TaskOutput:
    return TaskOutput(description="d", raw="{}", agent="A",
                      pydantic=ReportDraft(title=title,
                                           body_markdown=text if text is not None else body()))


def reviewed_output(title="Should Northbridge Invest in X?", text=None,
                    notes=None) -> TaskOutput:
    return TaskOutput(description="d", raw="{}", agent="A",
                      pydantic=ReviewedReport(
                          title=title,
                          body_markdown=text if text is not None else body(),
                          revision_notes=notes if notes is not None else []))


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------

def test_well_formed_draft_passes():
    passed, result = validate_report_draft(draft_output())
    assert passed is True
    assert isinstance(result, TaskOutput)


def test_missing_section_is_rejected():
    """AnalysisResult's fields map onto these sections one-to-one, so a missing
    section means content was silently dropped."""
    passed, feedback = validate_report_draft(
        draft_output(text=body([s for s in SECTIONS if "Capital" not in s]))
    )
    assert passed is False
    assert "Where Capital Is Flowing" in feedback


def test_sections_out_of_order_are_rejected():
    shuffled = [SECTIONS[4], SECTIONS[0], SECTIONS[1], SECTIONS[2], SECTIONS[3]]
    passed, feedback = validate_report_draft(draft_output(text=body(shuffled)))
    assert passed is False
    assert "out of order" in feedback


def test_competitive_landscape_matches_the_guides_longer_heading():
    """The guide's actual heading is 'Competitive Landscape: Leaders & New
    Entrants'; a prefix match has to accept it."""
    passed, _ = validate_report_draft(draft_output())
    assert passed is True


def test_optional_disagreement_section_is_allowed_but_not_required():
    """'Where Sources Disagree' is included only when tension is real, so it
    must neither be required nor rejected."""
    without = validate_report_draft(draft_output())
    assert without[0] is True

    with_section = SECTIONS[:4] + ["## Where Sources Disagree"] + SECTIONS[4:]
    passed, feedback = validate_report_draft(draft_output(text=body(with_section)))
    assert passed is True, feedback


def test_empty_title_is_rejected():
    passed, feedback = validate_report_draft(draft_output(title="  "))
    assert passed is False
    assert "title is empty" in feedback


def test_empty_body_is_rejected():
    passed, feedback = validate_report_draft(draft_output(text="   "))
    assert passed is False
    assert "body_markdown is empty" in feedback


# ---------------------------------------------------------------------------
# The Sources rule - correctness, not style
# ---------------------------------------------------------------------------

def test_hand_written_sources_section_is_rejected():
    """The appendix is generated from the verified claim list. A model writing
    its own Sources section is writing URLs from memory, and nothing
    downstream re-reads them - this is the one spot where a fabricated
    citation would slip past every other check."""
    passed, feedback = validate_report_draft(
        draft_output(text=body() + "\n\n## Sources\n- https://invented.test/article")
    )
    assert passed is False
    assert "Sources" in feedback and "appended automatically" in feedback


def test_model_written_markdown_table_is_rejected():
    """Same class of rule as the Sources ban. A live run had the model rewrite
    the funding table from the underlying claims, reporting a round in the
    wrong currency; the verified table is inserted by Python instead."""
    passed, feedback = validate_report_draft(
        draft_output(text=body(extra="\n\n| Company | Round |\n| --- | --- |\n| Acme | Seed |"))
    )
    assert passed is False
    assert "markdown table" in feedback
    assert "inserted" in feedback


# ---------------------------------------------------------------------------
# Tone and length
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("phrase", [
    "revolutionary", "game-changing", "poised to disrupt", "paradigm shift",
])
def test_promotional_language_is_rejected(phrase):
    passed, feedback = validate_report_draft(
        draft_output(text=body(extra=f"\n\nThis is a {phrase} technology."))
    )
    assert passed is False
    assert "promotional language" in feedback


def test_promotional_check_is_case_insensitive():
    passed, _ = validate_report_draft(
        draft_output(text=body(extra="\n\nA REVOLUTIONARY design."))
    )
    assert passed is False


def test_stub_length_is_rejected():
    passed, feedback = validate_report_draft(draft_output(text=body(filler_words=BELOW_BAND)))
    assert passed is False
    assert "too thin" in feedback


def test_runaway_length_is_rejected_with_a_concrete_cut_target():
    """Telling the writer how many words to cut is what makes the retry
    actionable rather than another guess."""
    passed, feedback = validate_report_draft(draft_output(text=body(filler_words=ABOVE_BAND)))
    assert passed is False
    assert "800-1100" in feedback
    assert "Cut roughly" in feedback
    # And it must not invite fixing the count by deleting a whole section.
    assert "do not drop a whole required section" in feedback


def test_length_band_allows_modest_overrun_but_not_a_sprawl():
    """The guide allows a briefing that needs more room to be honest, so a
    small overrun passes. A large one does not: with a loose ceiling a live run
    once shipped far over target while the Style Reviewer reported nothing to
    fix, which is exactly the case the gate has to catch."""
    # Over the house target, still inside the gate's band.
    assert validate_report_draft(
        draft_output(text=body(filler_words=JUST_UNDER_CEILING))
    )[0] is True
    # ~1600 words: well past the point a partner keeps reading.
    passed, feedback = validate_report_draft(draft_output(text=body(filler_words=ABOVE_BAND)))
    assert passed is False
    assert "Cut roughly" in feedback


# ---------------------------------------------------------------------------
# Reviewed output
# ---------------------------------------------------------------------------

def test_well_formed_review_passes():
    passed, _ = validate_reviewed_report(reviewed_output(notes=["Tightened summary."]))
    assert passed is True


def test_review_with_no_changes_is_valid():
    """A reviewer that finds nothing wrong should say so, not invent edits to
    look diligent."""
    passed, _ = validate_reviewed_report(reviewed_output(notes=[]))
    assert passed is True


def test_review_is_held_to_the_same_structural_rules():
    """A reviewer that 'fixed' the draft into an invalid shape is worse than
    one that changed nothing."""
    passed, feedback = validate_reviewed_report(
        reviewed_output(text=body([s for s in SECTIONS if "Capital" not in s]))
    )
    assert passed is False
    assert "Where Capital Is Flowing" in feedback


def test_empty_revision_note_is_rejected():
    passed, feedback = validate_reviewed_report(reviewed_output(notes=["Fixed.", "  "]))
    assert passed is False
    assert "revision_notes[1]" in feedback


# ---------------------------------------------------------------------------
# Unparsed output
# ---------------------------------------------------------------------------

def test_unparsed_draft_restates_the_schema():
    passed, feedback = validate_report_draft(
        TaskOutput(description="d", raw="junk", agent="A", pydantic=None)
    )
    assert passed is False
    assert "title" in feedback and "body_markdown" in feedback


def test_unparsed_review_restates_the_schema():
    passed, feedback = validate_reviewed_report(
        TaskOutput(description="d", raw="junk", agent="A", pydantic=None)
    )
    assert passed is False
    assert "revision_notes" in feedback
