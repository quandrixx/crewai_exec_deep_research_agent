"""
Project-local references for external_research/crew.jsonc.

CrewAI's JSON loader resolves `{"python": "..."}` refs through
_project_module_file(), which requires the target module to live inside the
directory holding crew.jsonc - a ref pointing directly at
`crewai_exec_deep_research_agent.models.ClaimList` is rejected with "Python
references in JSON configs must point to modules inside the project root".

So this file sits here purely to be resolvable, and re-exports the real
definitions from the package. The module is deliberately named for its crew
rather than something generic like `refs`, so it can never collide in
sys.modules with the internal crew's equivalent (the loader imports these as
top-level modules).

Note the asymmetry in crew.jsonc: `tools` entries point straight at the real
classes, because tool refs go through a different resolver with no such
restriction.
"""

from crewai_exec_deep_research_agent.crews.research_crew.research_guardrails import (  # noqa: F401
    validate_external_claims,
)
from crewai_exec_deep_research_agent.models import ClaimList  # noqa: F401
