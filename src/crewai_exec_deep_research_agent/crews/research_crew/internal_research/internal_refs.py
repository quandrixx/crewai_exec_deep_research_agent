"""
Project-local references for internal_research/crew.jsonc.

See external_research/external_refs.py for the full explanation - in short,
CrewAI's JSON loader only resolves `{"python": "..."}` refs to modules living
inside the directory that holds crew.jsonc, so the real definitions are
re-exported here rather than referenced directly in the package.
"""

from crewai_exec_deep_research_agent.crews.research_crew.research_guardrails import (  # noqa: F401
    validate_internal_claims,
)
from crewai_exec_deep_research_agent.models import ClaimList  # noqa: F401
