"""
test_internal_kb_tool.py

Tests the deterministic parts of internal_kb_tool: document loading/chunking,
keyword-overlap search, and the CrewAI tool wrapper's output formatting -
including its most important behavior, the explicit "no match" message,
which matters because a silent empty result could tempt an agent into
fabricating an internal view rather than stating plainly that Northbridge
has no prior work on the topic.

These tests run against the REAL mock docs in knowledge/internal_docs/,
not synthetic fixtures - intentional, since the whole point is to prove
retrieval actually works against the content the Research Crew will use.
Tests are written to check behavior (does the right doc show up, is the
result well-formed) rather than asserting on exact chunk text, so they
don't become brittle if the mock docs are edited later.
"""

import pytest #pyrefly: ignore
from crewai_exec_deep_research_agent.tools.internal_kb_tool import (
    InternalKBLookupTool,
    _CHUNKS,
    _search,
    _significant_words,
    _TOP_K,
)


# ---------------------------------------------------------------------------
# Document loading / chunking
# ---------------------------------------------------------------------------

def test_documents_actually_loaded():
    """Sanity check that the fixture docs are found at all - if this fails,
    every other test's failure is misleading, since it'd really be a path
    problem, not a retrieval-logic problem."""
    assert len(_CHUNKS) > 0


def test_all_five_mock_docs_are_represented():
    sources = {chunk["source"] for chunk in _CHUNKS}
    assert len(sources) == 5


def test_chunks_have_required_fields():
    for chunk in _CHUNKS:
        assert chunk["source"].endswith(".md")
        assert isinstance(chunk["heading"], str) and chunk["heading"]
        assert isinstance(chunk["text"], str) and chunk["text"].strip()


def test_headings_are_not_returned_as_chunks_themselves():
    """Markdown headers (lines starting with #) should be tracked as
    section labels, not returned as retrievable content chunks in their
    own right - a chunk whose entire text is just a heading is a bug."""
    for chunk in _CHUNKS:
        assert not chunk["text"].lstrip().startswith("#")


# ---------------------------------------------------------------------------
# _significant_words
# ---------------------------------------------------------------------------

def test_significant_words_strips_stopwords_and_short_words():
    words = _significant_words("This is a test of the geothermal drilling cost model")
    assert "geothermal" in words
    assert "drilling" in words
    assert "the" not in words
    assert "is" not in words
    assert "a" not in words  # too short, also a stopword


def test_significant_words_is_case_insensitive():
    assert _significant_words("Geothermal") == _significant_words("geothermal")


# ---------------------------------------------------------------------------
# _search - the actual retrieval logic
# ---------------------------------------------------------------------------

def test_search_finds_wave_energy_document():
    results = _search("wave energy offshore industrial customers")
    assert len(results) > 0
    assert results[0]["source"] == "internal_scouting_notes_wave_energy.md"


def test_search_finds_geothermal_document():
    results = _search("geothermal drilling cost")
    assert len(results) > 0
    assert any(r["source"] == "internal_diligence_enhanced_geothermal.md" for r in results)


def test_search_finds_advanced_nuclear_document():
    results = _search("SMR NRC licensing regulatory risk")
    assert len(results) > 0
    assert any(r["source"] == "internal_thesis_advanced_nuclear.md" for r in results)


def test_search_respects_top_k_limit():
    # A broad query likely to match many chunks across docs (e.g. "energy")
    results = _search("energy market")
    assert len(results) <= _TOP_K


def test_search_returns_empty_list_for_zero_overlap_query():
    """Confirmed via grep beforehand that none of these words appear in any
    mock doc - this is the genuine no-match case, not a weak-match case."""
    results = _search("employee dental insurance enrollment paperwork")
    assert results == []


def test_search_results_are_ranked_by_overlap_descending():
    results = _search("geothermal drilling directional oil gas cost")
    if len(results) > 1:
        scores = [
            len(_significant_words("geothermal drilling directional oil gas cost")
                & _significant_words(r["text"]))
            for r in results
        ]
        assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# InternalKBLookupTool - the CrewAI-facing wrapper
# ---------------------------------------------------------------------------

def test_tool_run_includes_source_attribution():
    tool = InternalKBLookupTool()
    output = tool._run("wave energy offshore industrial")
    assert "[source: internal_scouting_notes_wave_energy.md" in output


def test_tool_run_returns_explicit_no_match_message_for_unrelated_query():
    """This is the behavior that matters most: an agent seeing this exact
    message should conclude 'no prior internal work exists' rather than
    inferring an internal view doesn't exist just because output was empty,
    or worse, fabricating one."""
    tool = InternalKBLookupTool()
    output = tool._run("employee dental insurance enrollment paperwork")
    assert "No internal documents matched" in output
    assert "state that explicitly" in output


def test_tool_run_separates_multiple_results_clearly():
    tool = InternalKBLookupTool()
    output = tool._run("geothermal drilling cost diligence")
    if output.count("[source:") > 1:
        assert "---" in output  # the separator between chunks