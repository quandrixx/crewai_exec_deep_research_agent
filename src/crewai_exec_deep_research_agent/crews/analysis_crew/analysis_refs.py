"""
Project-local references for crew.jsonc.

CrewAI's JSON loader resolves `{"python": "..."}` refs through
_project_module_file(), which requires the target module to live inside the
directory holding crew.jsonc, and rejects a ref pointing directly at
`crewai_exec_deep_research_agent.models` even though the package is installed
and importable. So this module exists purely to be resolvable, and re-exports
the real definitions. Same pattern as the Research Crew's research_refs.py.

Note the asymmetry in crew.jsonc: `tools` entries point straight at real
classes, because tool refs go through a different resolver with no such
restriction.
"""

from crewai_exec_deep_research_agent.crews.analysis_crew.analysis_guardrails import (  # noqa: F401
    validate_investment_judgment,
    validate_landscape_analysis,
)
from crewai_exec_deep_research_agent.models import (  # noqa: F401
    InvestmentJudgment,
    LandscapeAnalysis,
)
