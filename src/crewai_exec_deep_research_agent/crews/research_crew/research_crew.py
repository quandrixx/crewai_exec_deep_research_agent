"""
Research Crew - stage one of the Deep Research Flow.

Conceptually one stage, implemented as two single-agent crews run at the same
time. That split is forced by CrewAI: Crew.validate_end_with_at_most_one_async_task
rejects a crew ending in more than one async task, and pairing an async task
with a following sync one doesn't help either, since _run_sequential_process
drains pending futures before starting the sync task. Two independent tasks
cannot actually run concurrently inside a single crew.

Keeping them concurrent is worth the extra directory. The internal and external
views must be gathered without either seeing the other, or the final report's
"Where Sources Disagree" section is measuring contamination rather than genuine
disagreement - and running them back-to-back would roughly double this stage's
wall time for no benefit.

Merging the two results is plain Python: it's list concatenation over
already-validated ClaimList objects, and routing it through a third agent task
would add cost and latency while creating a fresh opportunity for claims to be
silently reworded or dropped.
"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from crewai import Crew
from crewai.crews.crew_output import CrewOutput
from crewai.project import load_crew

from crewai_exec_deep_research_agent.models import ClaimList, ResearchFindings, SourcedClaim


_HERE = Path(__file__).parent
_EXTERNAL_CONFIG = _HERE / "external_research" / "crew.jsonc"
_INTERNAL_CONFIG = _HERE / "internal_research" / "crew.jsonc"

_EXTERNAL_TASK = "external_research_task"
_INTERNAL_TASK = "internal_research_task"


class ResearchCrew:
    """Gathers internal and external claims about a topic, in parallel."""

    def external_crew(self) -> Crew:
        crew, _default_inputs = load_crew(_EXTERNAL_CONFIG)
        return crew

    def internal_crew(self) -> Crew:
        crew, _default_inputs = load_crew(_INTERNAL_CONFIG)
        return crew

    def run(self, topic: str) -> ResearchFindings:
        """Research `topic` with both crews concurrently and merge the results.

        Threads rather than asyncio.gather + kickoff_async: this is called from
        inside a CrewAI Flow step, which may already own a running event loop,
        and asyncio.run() raises if one is active. CrewAI runs its own async
        tasks on threads for the same reason.
        """
        inputs = {"topic": topic}

        with ThreadPoolExecutor(max_workers=2) as pool:
            external_future = pool.submit(self.external_crew().kickoff, inputs=inputs)
            internal_future = pool.submit(self.internal_crew().kickoff, inputs=inputs)
            # .result() re-raises whatever the crew raised, on this thread. A
            # half-finished research stage should fail loudly rather than
            # quietly hand the Analysis Crew one side of the evidence.
            external_result = external_future.result()
            internal_result = internal_future.result()

        return ResearchFindings(
            topic=topic,
            external_claims=_claims_from(external_result, _EXTERNAL_TASK),
            internal_claims=_claims_from(internal_result, _INTERNAL_TASK),
        )


def _claims_from(result: CrewOutput, task_name: str) -> list[SourcedClaim]:
    """Pull one task's claims out of a crew result, by task name.

    Looked up by name rather than by index so that adding a task to either crew
    later can't silently start returning the wrong list - swapping internal and
    external claims would be both catastrophic and nearly invisible in the
    finished report.
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
