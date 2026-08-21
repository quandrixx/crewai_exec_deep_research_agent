"""
Research Crew loader - stage one of the Deep Research Flow.

One crew, two researchers running concurrently, plus a synchronization barrier
task that exists to satisfy CrewAI's "at most one trailing async task" rule.
See crew.jsonc for the full reasoning.

This module's job is the part that shouldn't be an LLM's: merging the two
tasks' outputs into a single ResearchFindings. That's list concatenation over
already-validated ClaimList objects, so routing it through a third agent task
would add cost and latency while creating a fresh opportunity for claims to be
silently reworded or dropped.

Failures here are loud. If a task produced no parsed output, that means the
agent's response never satisfied the ClaimList schema even after guardrail
retries - returning empty claims in that case would hand the Analysis Crew an
empty evidence base that looks exactly like a topic nobody has written about.
"""

from pathlib import Path

from crewai import Crew
from crewai.crews.crew_output import CrewOutput
from crewai.project import load_crew

from crewai_exec_deep_research_agent.models import ClaimList, ResearchFindings, SourcedClaim


_CREW_CONFIG = Path(__file__).parent / "crew.jsonc"

_EXTERNAL_TASK = "external_research_task"
_INTERNAL_TASK = "internal_research_task"


class ResearchCrew:
    """Gathers internal and external claims about a topic, in parallel."""

    def crew(self) -> Crew:
        """The configured Crew, for callers that want to drive kickoff directly."""
        crew, _default_inputs = load_crew(_CREW_CONFIG)
        return crew

    def run(self, topic: str) -> ResearchFindings:
        """Research `topic` and return the merged findings.

        This is the entry point the Flow uses. Prefer it over crew().kickoff():
        the crew produces two separate ClaimList outputs plus a barrier task's
        confirmation line, so CrewOutput.pydantic alone gives you none of what
        you actually want.
        """
        result = self.crew().kickoff(inputs={"topic": topic})

        return ResearchFindings(
            topic=topic,
            external_claims=_claims_from(result, _EXTERNAL_TASK),
            internal_claims=_claims_from(result, _INTERNAL_TASK),
        )


def _claims_from(result: CrewOutput, task_name: str) -> list[SourcedClaim]:
    """Pull one task's claims out of the crew result, by task name.

    Looked up by name rather than by index because async tasks complete in
    whatever order they finish, and silently swapping the internal and external
    claim lists would be both catastrophic and very hard to spot in a report.
    """
    for task_output in result.tasks_output:
        if task_output.name != task_name:
            continue
        claims = task_output.pydantic
        if not isinstance(claims, ClaimList):
            raise RuntimeError(
                f"Task '{task_name}' did not produce a valid ClaimList "
                f"(got {type(claims).__name__}). Its raw output was: "
                f"{(task_output.raw or '')[:500]}"
            )
        return claims.claims

    found = [task_output.name for task_output in result.tasks_output]
    raise RuntimeError(
        f"Task '{task_name}' produced no output. Tasks that did run: {found}. "
        f"This usually means a task name in crew.jsonc was changed without "
        f"updating research_crew.py."
    )
