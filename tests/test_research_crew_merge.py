"""
test_research_crew_merge.py

Tests ResearchCrew's orchestration - the parts that are deterministic Python
and therefore testable without spending an LLM call: that the two crews really
do run concurrently, and that merging their outputs keeps internal and external
claims on their own sides.

Both crews are stubbed. The point is not to test that agents research well, it
is to test the wiring around them, which is where a silent mistake (swapped
claim lists, a lost result) would be nearly invisible in a finished report.
"""

import threading
import time

import pytest  # pyrefly: ignore
from crewai.crews.crew_output import CrewOutput
from crewai.tasks.task_output import TaskOutput

from crewai_exec_deep_research_agent.crews.research_crew.research_crew import ResearchCrew
from crewai_exec_deep_research_agent.models import ClaimList, SourcedClaim, SourceType


def make_claim(text: str, source: str, source_type: SourceType) -> SourcedClaim:
    return SourcedClaim(claim=text, source=source, source_type=source_type, confidence=0.9)


def make_crew_output(task_name: str, claims: list[SourcedClaim]) -> CrewOutput:
    return CrewOutput(
        raw="{}",
        tasks_output=[
            TaskOutput(
                name=task_name,
                description="stub",
                raw="{}",
                agent="Stub Agent",
                pydantic=ClaimList(claims=claims),
            )
        ],
        token_usage={},
    )


class StubCrew:
    """Stands in for a loaded Crew, recording when its kickoff ran."""

    def __init__(self, task_name, claims, delay=0.0, recorder=None):
        self.task_name = task_name
        self.claims = claims
        self.delay = delay
        self.recorder = recorder

    def kickoff(self, inputs=None):
        if self.recorder is not None:
            self.recorder.append(("start", self.task_name, time.time()))
        time.sleep(self.delay)
        if self.recorder is not None:
            self.recorder.append(("end", self.task_name, time.time()))
        return make_crew_output(self.task_name, self.claims)


def install_stub_crews(monkeypatch, external: StubCrew, internal: StubCrew) -> None:
    monkeypatch.setattr(ResearchCrew, "external_crew", lambda self: external)
    monkeypatch.setattr(ResearchCrew, "internal_crew", lambda self: internal)


# ---------------------------------------------------------------------------
# Concurrency - the whole reason the stage is split into two crews
# ---------------------------------------------------------------------------

def test_both_crews_run_concurrently(monkeypatch):
    """The two halves must overlap in time. If this fails, the split into two
    crews has bought nothing over a single crew with two sequential tasks."""
    events: list[tuple[str, str, float]] = []
    delay = 0.3
    install_stub_crews(
        monkeypatch,
        StubCrew("external_research_task", [], delay=delay, recorder=events),
        StubCrew("internal_research_task", [], delay=delay, recorder=events),
    )
    # Both stubs return no claims, so bypass the external guardrail's rules by
    # checking timing only - claims content is covered by the merge tests below.
    start = time.time()
    ResearchCrew().run("some topic")
    elapsed = time.time() - start

    # Sequential execution would take at least 2 * delay.
    assert elapsed < delay * 1.8, f"crews appear to have run sequentially ({elapsed:.2f}s)"

    starts = {name: ts for kind, name, ts in events if kind == "start"}
    ends = {name: ts for kind, name, ts in events if kind == "end"}
    # Each crew started before the other finished - genuine overlap, not just
    # a fast total time.
    assert starts["external_research_task"] < ends["internal_research_task"]
    assert starts["internal_research_task"] < ends["external_research_task"]


def test_both_crews_receive_the_topic(monkeypatch):
    received: dict[str, dict] = {}

    class RecordingCrew(StubCrew):
        def kickoff(self, inputs=None):
            received[self.task_name] = inputs
            return make_crew_output(self.task_name, self.claims)

    install_stub_crews(
        monkeypatch,
        RecordingCrew("external_research_task", []),
        RecordingCrew("internal_research_task", []),
    )
    ResearchCrew().run("wave energy")

    assert received["external_research_task"] == {"topic": "wave energy"}
    assert received["internal_research_task"] == {"topic": "wave energy"}


# ---------------------------------------------------------------------------
# Merge correctness
# ---------------------------------------------------------------------------

def test_claims_are_merged_onto_the_correct_sides(monkeypatch):
    external = [make_claim("External fact.", "https://example.test/a", SourceType.EXTERNAL)]
    internal = [make_claim("Internal fact.", "internal_thesis.md", SourceType.INTERNAL)]
    install_stub_crews(
        monkeypatch,
        StubCrew("external_research_task", external),
        StubCrew("internal_research_task", internal),
    )

    findings = ResearchCrew().run("small modular reactors")

    assert findings.topic == "small modular reactors"
    assert [c.claim for c in findings.external_claims] == ["External fact."]
    assert [c.claim for c in findings.internal_claims] == ["Internal fact."]


def test_claims_are_looked_up_by_task_name_not_position(monkeypatch):
    """Async crews finish in whatever order they finish. Pulling results by
    index instead of name would eventually swap the two sides, which is both
    catastrophic and almost impossible to spot in a finished report."""
    external = [make_claim("External fact.", "https://example.test/a", SourceType.EXTERNAL)]
    internal = [make_claim("Internal fact.", "internal_thesis.md", SourceType.INTERNAL)]

    # The external crew emits an unrelated task's output FIRST; a positional
    # lookup would happily return that decoy.
    decoy = make_crew_output("some_other_task", internal)
    real = make_crew_output("external_research_task", external)
    combined = CrewOutput(
        raw="{}",
        tasks_output=[*decoy.tasks_output, *real.tasks_output],
        token_usage={},
    )

    class MultiTaskCrew(StubCrew):
        def kickoff(self, inputs=None):
            return combined

    install_stub_crews(
        monkeypatch,
        MultiTaskCrew("external_research_task", external),
        StubCrew("internal_research_task", internal),
    )

    findings = ResearchCrew().run("topic")
    assert [c.claim for c in findings.external_claims] == ["External fact."]


# ---------------------------------------------------------------------------
# Loud failure - never a silently empty research stage
# ---------------------------------------------------------------------------

def test_unparsed_task_output_raises_rather_than_returning_empty(monkeypatch):
    """A task whose output never parsed must not quietly become "no findings" -
    that hands the Analysis Crew an empty evidence base that looks exactly like
    a topic nobody has written about."""
    broken = CrewOutput(
        raw="not json",
        tasks_output=[
            TaskOutput(
                name="external_research_task",
                description="stub",
                raw="not json",
                agent="Stub Agent",
                pydantic=None,
            )
        ],
        token_usage={},
    )

    class BrokenCrew(StubCrew):
        def kickoff(self, inputs=None):
            return broken

    install_stub_crews(
        monkeypatch,
        BrokenCrew("external_research_task", []),
        StubCrew("internal_research_task", []),
    )

    with pytest.raises(RuntimeError, match="did not produce a valid ClaimList"):
        ResearchCrew().run("topic")


def test_missing_task_output_raises_with_the_names_that_did_run(monkeypatch):
    install_stub_crews(
        monkeypatch,
        StubCrew("renamed_task", []),
        StubCrew("internal_research_task", []),
    )

    with pytest.raises(RuntimeError, match="produced no output"):
        ResearchCrew().run("topic")
