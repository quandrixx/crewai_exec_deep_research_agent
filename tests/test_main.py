"""
test_main.py

Tests the CLI entry point's deterministic behavior: artifact persistence,
key checking, topic listing, and the demo topic file.

The artifact tests carry the most weight. The Flow withholds the report when
the fact-check gate fails twice, and the stated purpose of that path is to give
a human analyst a head start rather than making them restart the topic - which
only works if the research and analysis that produced the failed citations are
actually written to disk. An earlier ad-hoc runner saved artifacts only on the
success path and lost a full research run to exactly this.
"""

import json
import os
from pathlib import Path

import pytest  # pyrefly: ignore

from crewai_exec_deep_research_agent import main
from crewai_exec_deep_research_agent.flows.deep_research_flow import ResearchState
from crewai_exec_deep_research_agent.models import (
    AnalysisResult,
    CitationIssue,
    FactCheckResult,
    FinalReport,
    MarketShift,
    Recommendation,
    RecommendationAction,
    ResearchFindings,
    SourcedClaim,
    SourceType,
)


def claim(text="A claim.", source="https://example.test/a") -> SourcedClaim:
    return SourcedClaim(claim=text, source=source,
                        source_type=SourceType.EXTERNAL, confidence=0.9)


def research() -> ResearchFindings:
    return ResearchFindings(topic="t", external_claims=[claim()], internal_claims=[])


def analysis() -> AnalysisResult:
    return AnalysisResult(
        topic="t", executive_summary="Summary.",
        market_shifts=[MarketShift(description="A shift.", supporting_claim_indices=[0])],
        incumbents=[], new_entrants=[], funding_events=[], tensions_or_conflicts=[],
        recommendations=[Recommendation(action=RecommendationAction.MONITOR,
                                        text="Monitor.", supporting_claim_indices=[0])],
        all_claims=[claim()],
    )


def report() -> FinalReport:
    return FinalReport(
        title="Should We Invest?", executive_summary="Summary.",
        body_markdown="## Executive Summary\nText.",
        sources_appendix=["https://example.test/a"], fact_check_status="passed",
    )


# ---------------------------------------------------------------------------
# Artifact persistence
# ---------------------------------------------------------------------------

def test_successful_run_writes_every_artifact(tmp_path):
    state = ResearchState(
        topic="t", research=research(), analysis=analysis(),
        fact_check=FactCheckResult(passed=True, verified_count=2, issues=[]),
        final_report=report(),
    )
    written = main._save_artifacts(state, tmp_path)

    names = {p.name for p in written}
    assert names == {"research.json", "analysis.json", "fact_check.json",
                     "report.json", "report.md"}
    # The markdown is the rendered document, appendix included.
    assert "## Sources" in (tmp_path / "report.md").read_text()


def test_escalated_run_still_saves_the_work_that_led_to_the_failure(tmp_path):
    """The escalation path's whole value is the head start it gives a human.
    Printing the failed citations and discarding the research behind them
    would make a reviewer restart the topic from nothing."""
    state = ResearchState(
        topic="t", research=research(), analysis=analysis(),
        fact_check=FactCheckResult(
            passed=False, verified_count=1,
            issues=[CitationIssue(claim_or_entity="A rec", problem="cites claim 99")],
        ),
        final_report=None,
        revision_count=1,
    )
    written = main._save_artifacts(state, tmp_path)

    names = {p.name for p in written}
    assert "research.json" in names
    assert "analysis.json" in names
    # And the specific failures, so the analyst knows where to look.
    assert "fact_check.json" in names
    saved = json.loads((tmp_path / "fact_check.json").read_text())
    assert saved["issues"][0]["problem"] == "cites claim 99"
    # No report was produced, so none is written.
    assert "report.json" not in names
    assert "report.md" not in names


def test_run_that_failed_during_research_writes_nothing_rather_than_erroring(tmp_path):
    written = main._save_artifacts(ResearchState(topic="t"), tmp_path)
    assert written == []


def test_saved_artifacts_round_trip_back_into_their_models(tmp_path):
    """Saved output is meant to be reusable as a fixture for developing a
    downstream stage without paying for the upstream ones."""
    state = ResearchState(topic="t", research=research(), analysis=analysis(),
                          final_report=report())
    main._save_artifacts(state, tmp_path)

    assert ResearchFindings.model_validate_json((tmp_path / "research.json").read_text())
    assert AnalysisResult.model_validate_json((tmp_path / "analysis.json").read_text())
    assert FinalReport.model_validate_json((tmp_path / "report.json").read_text())


# ---------------------------------------------------------------------------
# Crash handling - completed stages must survive
# ---------------------------------------------------------------------------

def test_a_crash_mid_run_still_saves_the_completed_stages(monkeypatch, tmp_path, capsys):
    """A failure in a late stage must not discard several minutes of research.
    This is not hypothetical: an API credit exhaustion during the report stage
    is exactly how it came up."""
    class ExplodingFlow:
        def __init__(self):
            self.state = ResearchState(topic="t", research=research(), analysis=analysis())

        def kickoff(self, inputs=None):
            raise RuntimeError("credit balance is too low")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("SERPER_API_KEY", "k")
    monkeypatch.setattr(main, "DeepResearchFlow", ExplodingFlow)
    monkeypatch.setattr(main, "OUTPUT_DIR", tmp_path)

    code = main._run("t")

    assert code == main.EXIT_ERROR
    saved = {p.name for p in (tmp_path / "t").iterdir()}
    assert "research.json" in saved and "analysis.json" in saved

    err = capsys.readouterr().err
    assert "credit balance is too low" in err
    assert "still saved" in err


def test_a_crash_before_anything_completed_says_so(monkeypatch, tmp_path, capsys):
    class ImmediatelyExplodingFlow:
        def __init__(self):
            self.state = ResearchState(topic="t")

        def kickoff(self, inputs=None):
            raise RuntimeError("boom")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("SERPER_API_KEY", "k")
    monkeypatch.setattr(main, "DeepResearchFlow", ImmediatelyExplodingFlow)
    monkeypatch.setattr(main, "OUTPUT_DIR", tmp_path)

    assert main._run("t") == main.EXIT_ERROR
    assert "nothing was saved" in capsys.readouterr().err


def test_exit_code_distinguishes_escalation_from_success(monkeypatch, tmp_path):
    """A caller scripting this needs to tell 'no report' apart from 'crashed'."""
    class Flow:
        def __init__(self, produce_report):
            self.state = ResearchState(
                topic="t", research=research(), analysis=analysis(),
                final_report=report() if produce_report else None,
            )

        def kickoff(self, inputs=None):
            return None

    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("SERPER_API_KEY", "k")
    monkeypatch.setattr(main, "OUTPUT_DIR", tmp_path)

    monkeypatch.setattr(main, "DeepResearchFlow", lambda: Flow(True))
    assert main._run("t") == main.EXIT_OK

    monkeypatch.setattr(main, "DeepResearchFlow", lambda: Flow(False))
    assert main._run("t") == main.EXIT_ESCALATED


# ---------------------------------------------------------------------------
# API key handling
# ---------------------------------------------------------------------------

def test_missing_model_key_stops_before_doing_any_work(monkeypatch):
    """Fail fast rather than a minute into the first crew."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(SystemExit, match="ANTHROPIC_API_KEY"):
        main._check_api_keys()


def test_missing_search_key_warns_but_proceeds(monkeypatch, capsys):
    """A run without web search still produces a briefing from internal
    sources - weaker, but valid - so this is a warning, not a stop."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.delenv("SERPER_API_KEY", raising=False)

    main._check_api_keys()

    assert "SERPER_API_KEY" in capsys.readouterr().err


def test_both_keys_present_is_silent(monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("SERPER_API_KEY", "test-key")

    main._check_api_keys()

    assert capsys.readouterr().err == ""


# ---------------------------------------------------------------------------
# Topic handling
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("topic,expected", [
    ("small modular reactors", "small_modular_reactors"),
    ("wave and tidal energy", "wave_and_tidal_energy"),
    ("Enhanced Geothermal!", "enhanced_geothermal"),
])
def test_topics_become_safe_directory_names(topic, expected):
    assert main._slug(topic) == expected


def test_list_topics_exits_cleanly(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["kickoff", "--list-topics"])
    monkeypatch.setattr(main, "_load_env", lambda: None)

    with pytest.raises(SystemExit) as exc:
        main.kickoff()

    assert exc.value.code == main.EXIT_OK
    assert "small modular reactors" in capsys.readouterr().out


def test_topic_comes_from_the_command_line(monkeypatch):
    captured = {}
    monkeypatch.setattr("sys.argv", ["kickoff", "wave", "and", "tidal", "energy"])
    monkeypatch.setattr(main, "_load_env", lambda: None)
    monkeypatch.setattr(main, "_run", lambda topic: captured.setdefault("topic", topic) and 0)

    with pytest.raises(SystemExit):
        main.kickoff()

    assert captured["topic"] == "wave and tidal energy"


def test_zero_argument_run_uses_a_default(monkeypatch):
    """`crewai run` invokes the script with no arguments, so the zero-config
    path has to work."""
    captured = {}
    monkeypatch.setattr("sys.argv", ["kickoff"])
    monkeypatch.delenv("RESEARCH_TOPIC", raising=False)
    monkeypatch.setattr(main, "_load_env", lambda: None)
    monkeypatch.setattr(main, "_run", lambda topic: captured.setdefault("topic", topic) and 0)

    with pytest.raises(SystemExit):
        main.kickoff()

    assert captured["topic"] == main.DEFAULT_TOPIC


# ---------------------------------------------------------------------------
# Trigger payload (CrewAI AMP)
# ---------------------------------------------------------------------------

def test_trigger_payload_topic_is_used(monkeypatch):
    captured = {}
    monkeypatch.setattr("sys.argv", ["run_with_trigger", '{"topic": "molten salt reactors"}'])
    monkeypatch.setattr(main, "_load_env", lambda: None)
    monkeypatch.setattr(main, "_run", lambda topic: captured.setdefault("topic", topic) and 0)

    with pytest.raises(SystemExit):
        main.run_with_trigger()

    assert captured["topic"] == "molten salt reactors"


def test_malformed_trigger_payload_is_reported_clearly(monkeypatch):
    monkeypatch.setattr("sys.argv", ["run_with_trigger", "not json"])
    monkeypatch.setattr(main, "_load_env", lambda: None)

    with pytest.raises(SystemExit, match="not valid JSON"):
        main.run_with_trigger()


def test_missing_trigger_payload_is_reported_clearly(monkeypatch):
    monkeypatch.setattr("sys.argv", ["run_with_trigger"])
    monkeypatch.setattr(main, "_load_env", lambda: None)

    with pytest.raises(SystemExit, match="No trigger payload"):
        main.run_with_trigger()


# ---------------------------------------------------------------------------
# The demo topic file
# ---------------------------------------------------------------------------

def test_every_demo_topic_names_an_internal_document_that_exists():
    """topics.json exists so a reviewer can pick a run that demonstrates
    something specific, and each entry names the internal document that gives
    that run something to disagree with. A renamed document would quietly turn
    a chosen demo into a bland one."""
    from crewai_exec_deep_research_agent.tools.internal_kb_tool import (
        known_document_names,
    )

    data = json.loads(main.TOPICS_FILE.read_text())
    real_docs = known_document_names()

    for entry in data["topics"]:
        context = entry["internal_context"]
        if context.startswith("("):  # deliberately has no internal context
            continue
        assert context in real_docs, f"{entry['topic']} points at a missing document"


def test_suggested_topics_survive_a_missing_topics_file(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "TOPICS_FILE", tmp_path / "nope.json")
    assert main._suggested_topics() == [main.DEFAULT_TOPIC]


def test_suggested_topics_survive_a_corrupt_topics_file(monkeypatch, tmp_path):
    broken = tmp_path / "topics.json"
    broken.write_text("{not json")
    monkeypatch.setattr(main, "TOPICS_FILE", broken)
    assert main._suggested_topics() == [main.DEFAULT_TOPIC]
