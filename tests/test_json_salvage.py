"""
test_json_salvage.py

Tests recovering a JSON object from an agent's free-text answer.

This is a cost and reliability mechanism, not a convenience. A guardrail
rejection makes CrewAI call `agent.execute_task()` again — a fresh ReAct loop
that re-runs every tool call — and `guardrail_max_retries` is a hard ceiling
that raises when exhausted. So the two properties that matter are opposites of
each other: unwrap anything genuinely recoverable, and refuse to invent
structure for anything that isn't. Silently "fixing" a truncated payload would
resurrect the bug that once discarded 15 real claims.
"""

import pytest  # pyrefly: ignore

from crewai_exec_deep_research_agent.json_salvage import salvage_json_object


def salvage(raw):
    return salvage_json_object(raw, required_key="claims")


# ---------------------------------------------------------------------------
# What it should recover
# ---------------------------------------------------------------------------

def test_bare_json_object_is_returned_as_is():
    result = salvage('{"claims": [{"claim": "A."}]}')
    assert result is not None
    _, parsed = result
    assert parsed["claims"][0]["claim"] == "A."


def test_markdown_fenced_json_is_unwrapped():
    """The most common real failure: correct JSON inside a code fence."""
    raw = 'Here are my findings:\n```json\n{"claims": [{"claim": "A."}]}\n```'
    result = salvage(raw)
    assert result is not None
    assert result[1]["claims"][0]["claim"] == "A."


def test_prose_before_and_after_is_stripped():
    raw = 'I ran six searches.\n{"claims": []}\nLet me know if you need more.'
    result = salvage(raw)
    assert result is not None
    assert result[1]["claims"] == []


def test_returned_text_is_the_json_alone():
    """CrewAI re-exports the returned string through output_pydantic, so it has
    to be the payload and nothing else — returning the prose-wrapped original
    would fail to parse exactly as before."""
    raw = 'Findings below.\n```json\n{"claims": [{"claim": "A."}]}\n```'
    text, _ = salvage(raw)
    assert text.startswith("{") and text.endswith("}")


def test_braces_inside_claim_text_do_not_break_extraction():
    """A brace-counting scan has to be string-aware, or a claim mentioning
    JSON, code, or a set literal truncates the span."""
    raw = '{"claims": [{"claim": "The config uses {\\"mode\\": \\"fast\\"} by default."}]}'
    result = salvage(raw)
    assert result is not None
    assert "mode" in result[1]["claims"][0]["claim"]


def test_nested_objects_are_handled():
    raw = '{"claims": [{"claim": "A.", "meta": {"nested": {"deep": true}}}]}'
    result = salvage(raw)
    assert result is not None
    assert result[1]["claims"][0]["meta"]["nested"]["deep"] is True


def test_outermost_object_wins_over_a_nested_lookalike():
    """An answer can contain several JSON-ish fragments; the payload is the
    whole object, not an inner one that happens to share the key."""
    raw = '{"claims": [{"claim": "A.", "echo": {"claims": ["wrong"]}}]}'
    _, parsed = salvage(raw)
    assert parsed["claims"][0]["claim"] == "A."


# ---------------------------------------------------------------------------
# What it must refuse to recover
# ---------------------------------------------------------------------------

def test_truncated_json_is_not_salvaged():
    """The critical negative case. A cut-off payload must fail loudly rather
    than be repaired into a plausible-looking partial result."""
    raw = '{"claims": [{"claim": "A.", "source": "https://example.test/a", "conf'
    assert salvage(raw) is None


def test_object_without_the_required_key_is_ignored():
    """Guards against grabbing an unrelated object — a tool call the agent
    echoed, or an example it quoted."""
    assert salvage('{"query": "geothermal drilling costs 2026"}') is None


def test_prose_with_no_json_at_all_returns_none():
    assert salvage("I could not complete the research for this topic.") is None


def test_empty_and_missing_input_return_none():
    assert salvage("") is None
    assert salvage(None) is None


def test_malformed_json_is_not_guessed_at():
    assert salvage("{'claims': ['single quotes are not json']}") is None


@pytest.mark.parametrize("raw", [
    '{"claims": [}',
    '{"claims"}',
    '{{"claims": []}',
])
def test_structurally_broken_payloads_return_none_or_parse_cleanly(raw):
    """Never raise on hostile input — a crash here would turn a recoverable
    guardrail failure into a dead run."""
    result = salvage(raw)
    assert result is None or isinstance(result[1], dict)
