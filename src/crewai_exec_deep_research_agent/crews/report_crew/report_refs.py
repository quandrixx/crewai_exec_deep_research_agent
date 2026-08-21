"""
Project-local references for crew.jsonc.

CrewAI's JSON loader only resolves `{"python": "..."}` refs to modules inside
the directory holding crew.jsonc, so the real definitions are re-exported here.
Same pattern as research_refs.py and analysis_refs.py.
"""

from crewai_exec_deep_research_agent.crews.report_crew.report_guardrails import (  # noqa: F401
    validate_report_draft,
    validate_reviewed_report,
)
from crewai_exec_deep_research_agent.models import (  # noqa: F401
    ReportDraft,
    ReviewedReport,
)
