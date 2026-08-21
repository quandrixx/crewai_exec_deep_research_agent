"""
test_research_crew_merge.py

Tests ResearchCrew's orchestration - the parts that are deterministic Python
and therefore testable without spending an LLM call.

Two layers here:

  1. Merge behavior, with the whole crew stubbed. Verifies that claims land on
     the correct sides and that a task whose output never parsed fails loudly
     instead of quietly becoming "no findings".

  2. Concurrency, against the REAL crew.jsonc with only the agents' LLMs
     swapped for stubs. This is the test that protects the crew's structure:
     the two research tasks must be async and must be followed by a
     synchronous barrier task, because CrewAI rejects a crew ending in more
     than one async task. Get that ordering wrong and the crew either refuses
     to construct or silently runs the two researchers back-to-back, which no
     amount of unit-testing the merge would catch.
"""

import json
import time
from typing import ClassVar

import pytest  # pyrefly: ignore
from crewai.crews.crew_output import CrewOutput
from crewai.llms.base_llm import BaseLLM
from crewai.tasks.task_output import TaskOutput

from crewai_exec_deep_research_agent.crews.research_crew.research_crew import ResearchCrew
from crewai_exec_deep_research_agent.models import ClaimList, SourcedClaim, SourceType
from crewai_exec_deep_research_agent.tools.internal_kb_tool import known_document_names


EXTERNAL_TASK = "external_research_task"
INTERNAL_TASK = "internal_research_task"
BARRIER_TASK = "research_synchronization_task"


def make_claim(text: str, source: str, source_type: SourceType) -> SourcedClaim:
    return SourcedClaim(claim=text, source=source, source_type=source_type, confidence=0.9)


def make_crew_output(*named_claims: tuple[str, list[SourcedClaim]]) -> CrewOutput:
    return CrewOutput(
        raw="{}",
        tasks_output=[
            TaskOutput(
                name=name,
                description="stub",
                raw="{}",
                agent="Stub Agent",
                pydantic=ClaimList(claims=claims),
            )
            for name, claims in named_claims
        ],
        token_usage={},
    )


class StubCrew:
    """Stands in for the loaded Crew."""

    def __init__(self, output: CrewOutput):
        self.output = output
        self.received_inputs = None

    def kickoff(self, inputs=None):
        self.received_inputs = inputs
        return self.output


# ---------------------------------------------------------------------------
# Merge behavior
# ---------------------------------------------------------------------------

def test_claims_are_merged_onto_the_correct_sides(monkeypatch):
    external = [make_claim("External fact.", "https://example.test/a", SourceType.EXTERNAL)]
    internal = [make_claim("Internal fact.", "internal_thesis.md", SourceType.INTERNAL)]
    stub = StubCrew(make_crew_output((EXTERNAL_TASK, external), (INTERNAL_TASK, internal)))
    monkeypatch.setattr(ResearchCrew, "crew", lambda self: stub)

    findings = ResearchCrew().run("small modular reactors")

    assert findings.topic == "small modular reactors"
    assert [c.claim for c in findings.external_claims] == ["External fact."]
    assert [c.claim for c in findings.internal_claims] == ["Internal fact."]
    assert stub.received_inputs == {"topic": "small modular reactors"}


def test_claims_are_looked_up_by_task_name_not_position(monkeypatch):
    """Async tasks finish in whatever order they finish, and the barrier task's
    output sits in the same list. Pulling results by index would eventually
    swap the two sides - catastrophic, and almost invisible in a report."""
    external = [make_claim("External fact.", "https://example.test/a", SourceType.EXTERNAL)]
    internal = [make_claim("Internal fact.", "internal_thesis.md", SourceType.INTERNAL)]
    # Internal completed first, and an unrelated task leads the list.
    stub = StubCrew(make_crew_output(
        ("some_other_task", []),
        (INTERNAL_TASK, internal),
        (EXTERNAL_TASK, external),
    ))
    monkeypatch.setattr(ResearchCrew, "crew", lambda self: stub)

    findings = ResearchCrew().run("topic")

    assert [c.claim for c in findings.external_claims] == ["External fact."]
    assert [c.claim for c in findings.internal_claims] == ["Internal fact."]


def test_unparsed_task_output_raises_rather_than_returning_empty(monkeypatch):
    """A task whose output never parsed must not quietly become "no findings" -
    that hands the Analysis Crew an empty evidence base that looks exactly like
    a topic nobody has written about."""
    broken = CrewOutput(
        raw="not json",
        tasks_output=[
            TaskOutput(
                name=EXTERNAL_TASK,
                description="stub",
                raw="not json",
                agent="Stub Agent",
                pydantic=None,
            )
        ],
        token_usage={},
    )
    monkeypatch.setattr(ResearchCrew, "crew", lambda self: StubCrew(broken))

    with pytest.raises(RuntimeError, match="did not produce a valid ClaimList"):
        ResearchCrew().run("topic")


def test_missing_task_output_raises_naming_the_tasks_that_did_run(monkeypatch):
    stub = StubCrew(make_crew_output(("renamed_task", [])))
    monkeypatch.setattr(ResearchCrew, "crew", lambda self: stub)

    with pytest.raises(RuntimeError, match="produced no output"):
        ResearchCrew().run("topic")


# ---------------------------------------------------------------------------
# Structure and concurrency, against the real crew.jsonc
# ---------------------------------------------------------------------------

def test_real_config_has_two_async_tasks_followed_by_a_sync_barrier():
    """The exact shape CrewAI requires. Two async tasks alone fail Crew's
    validate_end_with_at_most_one_async_task; the trailing sync task is what
    makes the concurrent pair legal."""
    tasks = ResearchCrew().crew().tasks

    assert [t.name for t in tasks] == [EXTERNAL_TASK, INTERNAL_TASK, BARRIER_TASK]
    assert tasks[0].async_execution is True
    assert tasks[1].async_execution is True
    assert tasks[2].async_execution is False


def test_research_tasks_do_not_depend_on_each_other():
    """A context link between them would serialize the pair and let each
    researcher see the other's findings - which would make the report's
    'Where Sources Disagree' section meaningless."""
    external, internal, _barrier = ResearchCrew().crew().tasks

    for task in (external, internal):
        assert not isinstance(task.context, list) or task.context == []


class ScriptedLLM(BaseLLM):
    """A stand-in model: records when it ran, sleeps, returns canned output.

    Lets the real crew execute end-to-end - real config, real guardrails, real
    merge - without any network calls.
    """

    label: str = ""
    payload: str = ""
    work_seconds: float = 0.4

    # ClassVar, not a field: a `list` field would be validated into a separate
    # copy per instance, so the three stand-ins would each record into their
    # own list and the timeline would come back empty.
    events: ClassVar[list[tuple[str, str, float]]] = []

    def call(self, messages, tools=None, callbacks=None, available_functions=None,
             from_task=None, from_agent=None, **kwargs) -> str:
        ScriptedLLM.events.append((self.label, "start", time.time()))
        time.sleep(self.work_seconds)
        ScriptedLLM.events.append((self.label, "end", time.time()))
        return self.payload

    def supports_function_calling(self) -> bool:
        return False


def build_scripted_crew():
    """The real crew, with each agent's LLM replaced by a scripted stand-in."""
    crew = ResearchCrew().crew()

    external_payload = json.dumps({"claims": [{
        "claim": "A named company raised $42M in March 2026.",
        "source": "https://example-news.test/funding",
        "source_type": "external",
        "confidence": 0.9,
    }]})
    internal_payload = json.dumps({"claims": [{
        "claim": "The firm previously reviewed this sector.",
        "source": sorted(known_document_names())[0],
        "source_type": "internal",
        "confidence": 0.9,
    }]})
    payloads = {
        "External Market Researcher": ("external", external_payload),
        "Internal Knowledge Analyst": ("internal", internal_payload),
        "Research Coordinator": ("barrier", "Research complete: 1 external, 1 internal."),
    }

    for agent in crew.agents:
        label, payload = payloads[agent.role]
        agent.llm = ScriptedLLM(model="stub", label=label, payload=payload)
    return crew


def test_the_two_research_tasks_actually_overlap_in_time():
    """The property the whole three-task structure exists to buy. Without the
    async flags this still passes its assertions on output, but takes twice as
    long and the overlap check fails."""
    ScriptedLLM.events.clear()
    crew = build_scripted_crew()

    crew.kickoff(inputs={"topic": "test topic"})

    starts = {label: ts for label, kind, ts in ScriptedLLM.events if kind == "start"}
    ends = {label: ts for label, kind, ts in ScriptedLLM.events if kind == "end"}

    # Each research task began before the other finished - genuine overlap,
    # not merely a fast total runtime.
    assert starts["external"] < ends["internal"]
    assert starts["internal"] < ends["external"]
    # The barrier ran only after both had finished; that's what makes it a
    # barrier rather than a third concurrent task.
    assert starts["barrier"] >= ends["external"]
    assert starts["barrier"] >= ends["internal"]


def test_real_config_runs_end_to_end_through_guardrails_and_merge(monkeypatch):
    """Exercises the whole path with no network: real task configs, real
    output_pydantic parsing, real guardrails, real merge."""
    ScriptedLLM.events.clear()
    crew = build_scripted_crew()
    monkeypatch.setattr(ResearchCrew, "crew", lambda self: crew)

    findings = ResearchCrew().run("test topic")

    assert findings.topic == "test topic"
    assert len(findings.external_claims) == 1
    assert len(findings.internal_claims) == 1
    assert findings.external_claims[0].source.startswith("https://")
    assert findings.internal_claims[0].source in known_document_names()
