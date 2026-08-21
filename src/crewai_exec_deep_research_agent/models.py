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
    PRE_SEED = "pre_seed"
    SEED = "seed"
    SERIES_A = "series_a"
    SERIES_B = "series_b"
    GROWTH = "growth"
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

class FinalReport(BaseModel):
    title: str
    executive_summary: str
    body_markdown: str
    sources_appendix: list[str]
    fact_check_status: Literal["passed", "passed_with_flags", "failed"]