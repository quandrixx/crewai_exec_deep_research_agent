"""
citation_check_tool.py

Deterministic verification of AnalysisResult before it's allowed to reach
the Report Crew. Deliberately NOT an agent - the whole point of this gate
is that the thing deciding whether a report is trustworthy shouldn't be
the same kind of process (an LLM) that could hallucinate in the first
place. Everything here is plain Python: index lookups and keyword overlap,
nothing that calls out to a model.

Checks three things, one per structured entity type that carries a
claim reference:
  1. Recommendation.supporting_claim_indices  - the obvious one
  2. CompanyProfile.supporting_claim_indices  - incumbents + new entrants
  3. FundingEvent.source_claim_index          - funding amounts/dates are
     exactly the kind of specific, checkable detail worth verifying,
     since they're also the easiest thing for an LLM to quietly invent.

Two failure classes are distinguished:
  - STRUCTURAL: the cited index doesn't exist at all. Always a hard fail.
  - WEAK_SUPPORT: the indices exist, but NONE of the cited claims share any
    words with the thing citing them - a cheap, conservative signal that the
    citation set may be spurious (e.g. right ballpark, wrong specific fact).
    Evaluated across the whole citation set rather than claim by claim, since
    an entity's claims each tend to support a different part of it; see
    _check_weak_support for the live failure that established this.
    Deliberately conservative thresholds to minimize false positives; this
    is a narrow, bounded heuristic, not a claim of semantic verification.
    Swap in a narrowly-scoped LLM call here later if the heuristic proves
    too noisy or too lax - keep any such call scoped to a single
    claim/citation pair, not a free re-analysis of the whole report.
"""

import re
from crewai_exec_deep_research_agent.models import AnalysisResult, CitationIssue, FactCheckResult


_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with",
    "is", "are", "was", "were", "this", "that", "these", "those", "as",
    "by", "at", "it", "its", "be", "has", "have", "had", "will", "we",
    "our", "than", "into", "from", "not", "no",
}

# How much word overlap counts as "plausibly related" between a citing
# entity's text and the claim it points to. Kept low deliberately -
# this is meant to catch egregious mismatches, not enforce paraphrase quality.
_MIN_OVERLAP_WORDS = 1


def _significant_words(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {w for w in words if w not in _STOPWORDS and len(w) > 2}


def _overlap_count(a: str, b: str) -> int:
    return len(_significant_words(a) & _significant_words(b))


def _validate_index(
    index: int,
    max_index: int,
    label: str,
    citing_text: str,
    issues: list[CitationIssue],
) -> bool:
    """Returns True if the index is structurally valid."""
    if index < 0 or index > max_index:
        issues.append(CitationIssue(
            claim_or_entity=citing_text,
            problem=(
                f"{label} cites claim index {index}, which does not exist "
                f"(only {max_index + 1} claim(s) available)."
            ),
        ))
        return False
    return True


def _check_weak_support(
    citing_text: str,
    claim_texts: list[str],
    label: str,
    issues: list[CitationIssue],
) -> None:
    """Flag an entity only when NONE of its cited claims relate to it.

    Evaluated across the whole citation set, not per claim. An entity usually
    cites several claims that each support a different part of it - a company
    profile might cite one claim for the funding round and another for the
    technical differentiation - so demanding that EVERY cited claim share
    vocabulary with the citing text flags correct output constantly. A live
    end-to-end run escalated to human review for exactly this: a CorPower Ocean
    profile whose differentiation described cost reductions, citing a
    perfectly good claim about its Series B.

    A citation set where nothing relates to the citing text is still the
    egregious case this heuristic was written to catch, and it is still caught.
    """
    if not claim_texts:
        return
    if any(
        _overlap_count(citing_text, claim_text) >= _MIN_OVERLAP_WORDS
        for claim_text in claim_texts
    ):
        return
    issues.append(CitationIssue(
        claim_or_entity=citing_text,
        problem=(
            f"{label} cites {len(claim_texts)} claim(s), none with any shared "
            f"terminology (e.g. '{claim_texts[0][:80]}...') - possible "
            f"spurious citation."
        ),
    ))


def check_citations(analysis: AnalysisResult) -> FactCheckResult:
    issues: list[CitationIssue] = []
    verified = 0
    max_index = len(analysis.all_claims) - 1

    # -- 1. Recommendations --------------------------------------------
    for rec in analysis.recommendations:
        if not rec.supporting_claim_indices:
            issues.append(CitationIssue(
                claim_or_entity=rec.text,
                problem="Recommendation has no supporting claims cited.",
            ))
            continue
        cited: list[str] = []
        for idx in rec.supporting_claim_indices:
            if _validate_index(idx, max_index, "Recommendation", rec.text, issues):
                verified += 1
                cited.append(analysis.all_claims[idx].claim)
        _check_weak_support(rec.text, cited, "Recommendation", issues)

    # -- 2. Company profiles (incumbents + new entrants) ---------------
    for company in [*analysis.incumbents, *analysis.new_entrants]:
        label = f"Company profile for '{company.name}'"
        if not company.supporting_claim_indices:
            issues.append(CitationIssue(
                claim_or_entity=company.name,
                problem=f"{label} has no supporting claims cited.",
            ))
            continue
        cited = []
        for idx in company.supporting_claim_indices:
            if _validate_index(idx, max_index, label, company.name, issues):
                verified += 1
                cited.append(analysis.all_claims[idx].claim)
        # Name included in the citing text on purpose: a claim that names the
        # company plainly supports its profile, even when the differentiation
        # prose is about some other attribute entirely.
        _check_weak_support(
            f"{company.name} {company.differentiation}", cited, label, issues,
        )

    # -- 3. Funding events -----------------------------------------------
    for event in analysis.funding_events:
        label = f"Funding event for '{event.company_name}'"
        idx = event.source_claim_index
        if _validate_index(idx, max_index, label, event.company_name, issues):
            verified += 1
            event_text = (
                f"{event.company_name} {event.round.value} "
                f"{event.amount_usd or ''} {event.lead_investor or ''}"
            )
            # Single index by construction, so this is the one place the check
            # is genuinely per-claim.
            _check_weak_support(
                event_text, [analysis.all_claims[idx].claim], label, issues
            )

    return FactCheckResult(
        passed=len(issues) == 0,
        verified_count=verified,
        issues=issues,
    )