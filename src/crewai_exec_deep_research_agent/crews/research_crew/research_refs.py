"""
Project-local references for crew.jsonc.

CrewAI's JSON loader resolves `{"python": "..."}` refs through
_project_module_file(), which requires the target module to live inside the
directory holding crew.jsonc, and raises "Python references in JSON configs
must point to modules inside the project root" otherwise. A ref pointing
directly at `crewai_exec_deep_research_agent.models.ClaimList` is rejected even
though the package is installed and importable.

So this module exists purely to be resolvable, and re-exports the real
definitions from the package. Note the asymmetry in crew.jsonc: `tools` entries
point straight at the real classes, because tool refs go through a different
resolver with no such restriction.
"""

from crewai_exec_deep_research_agent.crews.research_crew.research_guardrails import (  # noqa: F401
    validate_external_claims,
    validate_internal_claims,
)
from crewai_exec_deep_research_agent.models import ClaimList  # noqa: F401
