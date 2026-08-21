from pydantic import BaseModel, Field
from typing import Literal
from enum import Enum

class SourceType(str, Enum):
    INTERNAL = "internal"
    EXTERNAL = "external"

class SourcedClaim(BaseModel):
    claim: str = Field(description="A single factual statement, one idea per claim.")
    source: str = Field(description="Document title, URL, or internal doc filename.")
    source_type: SourceType
    confidence: float = Field(ge=0, le=1)

class ResearchFindings(BaseModel):
    topic: str
    internal_claims: list[SourcedClaim]
    external_claims: list[SourcedClaim]

class Recommendation(BaseModel):
    text: str
    supporting_claim_indices: list[int]  # indices into ResearchFindings claims, forces traceability

class AnalysisResult(BaseModel):
    topic: str
    executive_summary: str
    key_findings: list[str]
    tensions_or_conflicts: list[str]     # where internal and external sources disagree
    recommendations: list[Recommendation]
    all_claims: list[SourcedClaim]       # carried through for the fact-check step

class CitationIssue(BaseModel):
    claim: str
    problem: str                          # e.g. "no matching source found", "source doesn't support claim"

class FactCheckResult(BaseModel):
    passed: bool
    verified_claim_count: int
    issues: list[CitationIssue]

class FinalReport(BaseModel):
    title: str
    executive_summary: str
    body_markdown: str
    sources_appendix: list[str]
    fact_check_status: Literal["passed", "passed_with_flags", "failed"]