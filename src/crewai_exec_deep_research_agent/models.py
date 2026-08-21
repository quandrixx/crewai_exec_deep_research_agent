"""
Data contracts for the Deep Research Agent.

Every crew boundary in the Flow passes one of these typed objects, never free text.
The shapes below are derived directly from style_guide.md's report structure -
each maps onto a specific field here so the Formatter agent isn't reverse-
engineering structure out of prose.
"""

from pydantic import BaseModel, Field
from typing import Literal
from enum import Enum


# ---------------------------------------------------------------------------
# Shared building blocks
# ---------------------------------------------------------------------------

class SourceType(str, Enum):
    INTERNAL = "internal"
    EXTERNAL = "external"


class SourcedClaim(BaseModel):
    """A single atomic fact, traceable to exactly one source."""
    claim: str = Field(description="One factual statement, one idea per claim.")
    source: str = Field(description="Document title, URL, or internal doc filename.")
    source_type: SourceType
    confidence: float = Field(ge=0, le=1)


class EntityType(str, Enum):
    INCUMBENT = "incumbent"
    NEW_ENTRANT = "new_entrant"


class FundingStage(str, Enum):
    """Funding stages, sized for capital-intensive deep tech.

    The later stages are not optional padding. Emerging energy companies raise
    far more, and for far longer, than a typical software startup - a live run
    on small modular reactors hit an X-energy Series D and a Fervo Energy IPO,
    and an earlier version of this enum that stopped at series_b failed
    validation on exactly that data. Project finance and debt are likewise
    normal here, not exotic: reactors and geothermal wells get built with it.
    """
    PRE_SEED = "pre_seed"
    SEED = "seed"
    SERIES_A = "series_a"
    SERIES_B = "series_b"
    SERIES_C = "series_c"
    SERIES_D = "series_d"
    SERIES_E = "series_e"
    GROWTH = "growth"
    DEBT = "debt"
    PUBLIC = "public"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Research Crew output
# ---------------------------------------------------------------------------

class ClaimList(BaseModel):
    """Output of a single Research Crew task.

    Exists only because CrewAI's output_pydantic requires a BaseModel, not a
    bare list[SourcedClaim]. Each of the two parallel research tasks emits one
    of these; the crew loader unpacks them into ResearchFindings below rather
    than spending a third LLM task on what is just a list concatenation.

    An empty claims list is valid and meaningful - it means the researcher
    genuinely found nothing, which is a better result than padded findings.

    'claims' is deliberately REQUIRED rather than defaulting to []. With a
    default, a truncated or malformed agent response still validates into an
    empty ClaimList, which makes "the model ran out of output tokens mid-JSON"
    indistinguishable from "there was nothing to find" - observed live, and it
    silently dropped 15 real sourced claims. Requiring the field turns that
    case into a parse failure the task guardrail can catch and retry.
    """
    claims: list[SourcedClaim]


class ResearchFindings(BaseModel):
    """Output of the Research Crew - raw gathered claims, no synthesis yet."""
    topic: str
    internal_claims: list[SourcedClaim]
    external_claims: list[SourcedClaim]


# ---------------------------------------------------------------------------
# Analysis Crew output - one field per style-guide section
# ---------------------------------------------------------------------------

class MarketShift(BaseModel):
    """Maps to the 'What's Changing' section."""
    description: str
    supporting_claim_indices: list[int] = Field(
        description="Indices into AnalysisResult.all_claims that support this."
    )


class CompanyProfile(BaseModel):
    """Maps to 'Competitive Landscape: Leaders & New Entrants'."""
    name: str
    entity_type: EntityType
    founded_year: int | None = None
    funding_stage: FundingStage = FundingStage.UNKNOWN
    differentiation: str = Field(
        description="What's actually distinct about this company's approach - "
                     "never just 'is active in this space'."
    )
    supporting_claim_indices: list[int]


class FundingEvent(BaseModel):
    """Maps to 'Where Capital Is Flowing'. Kept structured so the Report Crew
    can render this as a deterministic markdown table instead of asking an
    LLM to format numbers correctly from prose."""
    company_name: str
    round: FundingStage
    amount_usd: float | None = None
    date: str
    lead_investor: str | None = None
    source_claim_index: int


class RecommendationAction(str, Enum):
    SOURCE_DEALS = "source_deals"
    PRIORITIZE_DILIGENCE = "prioritize_diligence"
    MONITOR = "monitor"
    PASS = "pass"


class Recommendation(BaseModel):
    action: RecommendationAction
    text: str
    supporting_claim_indices: list[int] = Field(
        description="Forces traceability - every recommendation must point "
                     "at specific evidence, not just read as persuasive."
    )


class LandscapeAnalysis(BaseModel):
    """Output of the Analysis Crew's first task - what the evidence says.

    Split from InvestmentJudgment below rather than having one agent emit a
    whole AnalysisResult, for the same reason ClaimList.claims is required:
    a large nested payload is the shape that gets truncated mid-JSON. Two
    smaller outputs each stay well inside the model's output budget.
    """
    market_shifts: list[MarketShift]
    incumbents: list[CompanyProfile]
    new_entrants: list[CompanyProfile]
    funding_events: list[FundingEvent]


class InvestmentJudgment(BaseModel):
    """Output of the Analysis Crew's second task - what to do about it.

    Deliberately produced by a different agent than LandscapeAnalysis, reading
    the landscape as context. Organizing evidence and committing to a position
    are different jobs, and the style guide is emphatic that a briefing which
    only describes the landscape has failed at its actual purpose.
    """
    executive_summary: str
    tensions_or_conflicts: list[str] = Field(
        description="Empty list is valid and expected when internal/external "
                     "sources genuinely agree - do not manufacture tension."
    )
    recommendations: list[Recommendation]


class AnalysisResult(BaseModel):
    """Output of the Analysis Crew. Deliberately shaped to mirror the six
    body sections of style_guide.md one-to-one."""
    topic: str
    executive_summary: str
    market_shifts: list[MarketShift]
    incumbents: list[CompanyProfile]
    new_entrants: list[CompanyProfile]
    funding_events: list[FundingEvent]
    tensions_or_conflicts: list[str] = Field(
        default_factory=list,
        description="Empty list is valid and expected when internal/external "
                     "sources genuinely agree - do not manufacture tension."
    )
    recommendations: list[Recommendation]
    all_claims: list[SourcedClaim] = Field(
        description="Every claim from ResearchFindings, carried through so "
                     "the fact-check gate has something to verify indices against."
    )


# ---------------------------------------------------------------------------
# Fact-check gate
# ---------------------------------------------------------------------------

class CitationIssue(BaseModel):
    claim_or_entity: str = Field(description="The recommendation, company, or "
                                              "funding event text that failed.")
    problem: str


class FactCheckResult(BaseModel):
    passed: bool
    verified_count: int
    issues: list[CitationIssue]


# ---------------------------------------------------------------------------
# Report Crew output
# ---------------------------------------------------------------------------

class ReportDraft(BaseModel):
    """Output of the Report Crew's first task - the formatted briefing.

    Carries only what an LLM should be writing. The Sources appendix and the
    funding table are built in Python from structured data (see report_crew.py):
    they are the parts where an invented URL or a mistyped dollar figure would
    do the most damage and where there is nothing for a model to add.
    """
    title: str = Field(
        description="Framed as the investment question, not the topic - "
                     "'Should Northbridge Increase Sourcing Activity in X?' "
                     "rather than 'X Market Overview'."
    )
    body_markdown: str = Field(
        description="The full briefing in markdown, Executive Summary through "
                     "Investment Recommendation. No Sources section - that is "
                     "appended deterministically."
    )


class ReviewedReport(BaseModel):
    """Output of the Report Crew's second task - the same report after a style pass."""
    title: str
    body_markdown: str
    revision_notes: list[str] = Field(
        description="What the reviewer changed and why. An empty list is valid "
                     "and means the draft already met the style guide - do not "
                     "invent changes to look diligent."
    )


class FinalReport(BaseModel):
    title: str
    executive_summary: str
    body_markdown: str
    sources_appendix: list[str]
    fact_check_status: Literal["passed", "passed_with_flags", "failed"]