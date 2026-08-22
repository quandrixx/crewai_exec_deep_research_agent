"""
citation_check_tool.py

Deterministic verification of AnalysisResult before it's allowed to reach
the Report Crew. Deliberately NOT an agent - the whole point of this gate
is that the thing deciding whether a report is trustworthy shouldn't be
the same kind of process (an LLM) that could hallucinate in the first
place. Everything here is plain Python: index lookups and keyword overlap,
nothing that calls out to a model.

Checks four things, one per structured entity type that carries a
claim reference:
  1. Recommendation.supporting_claim_indices  - the obvious one
  2. CompanyProfile.supporting_claim_indices  - incumbents + new entrants
  3. FundingEvent.source_claim_index          - funding amounts/dates are
     exactly the kind of specific, checkable detail worth verifying,
     since they're also the easiest thing for an LLM to quietly invent.
  4. Tension.internal_claim_indices /
     Tension.external_claim_indices          - both sides of a claimed
     disagreement, checked for the one thing that makes it a disagreement:
     that each side cites claims of the type it says it does.

Three failure classes are distinguished:
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
  - MISATTRIBUTED: a tension citing a claim on the wrong side of the split -
    an 'internal' index pointing at an external claim, or vice versa. Always a
    hard fail: it is the difference between reporting a real disagreement and
    inventing one out of claims that never disagreed.
  - UNNAMED: a company profile whose cited claims never mention the company.
    Company profiles get this extra check because WEAK_SUPPORT is close to
    useless for them - see _check_company_is_named.
"""

import re
from crewai_exec_deep_research_agent.models import (
    AnalysisResult,
    CitationIssue,
    FactCheckResult,
    SourceType,
)


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

# A term appearing in at least this share of a run's claims is treated as
# sector vocabulary - shared by everything, so evidence of nothing. Used both
# for company-name identity and for prose overlap; they are different
# tokenizers asking the same question, so they share one threshold.
_MAX_TERM_DOC_FREQUENCY = 0.25

# How many distinctive terms a citing entity and a claim must share before the
# link is believed. Capped, not fixed: an entity with only one distinctive term
# to offer is held to one. See _check_weak_support.
_MAX_REQUIRED_TERMS = 2


def _significant_words(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {w for w in words if w not in _STOPWORDS and len(w) > 2}


def _overlap_count(a: str, b: str) -> int:
    return len(_significant_words(a) & _significant_words(b))


def _tokens(text: str) -> set[str]:
    """Raw alphanumeric tokens - deliberately NOT _significant_words.

    That helper drops anything three characters or shorter, which is right for
    prose overlap and wrong for company names: it erases 'BP' entirely.
    """
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _distinctive_name_tokens(name: str, claim_texts: list[str]) -> set[str]:
    """The parts of a company name that actually identify it in THIS corpus.

    Every claim in one run is about one sector, so a name's sector words -
    'Energy', 'Power', 'Hydrogen', 'SMR' - appear all over the corpus and
    identify nobody. Rather than hardcode a stoplist, which would have to be
    rewritten for every sector this tool is ever pointed at, a token counts as
    identifying only if it is rare across the run's own claims. Measured on the
    five saved runs, this keeps 'fervo', 'nuscale', 'orbital', 'corpower',
    'bp' and discards 'energy', 'power', 'hydrogen', 'smr', 'wave'.

    Returns an empty set when nothing in the name is distinctive enough to
    test - the caller treats that as "cannot judge", not as a failure.
    """
    if not claim_texts:
        return set()
    claim_tokens = [_tokens(text) for text in claim_texts]
    limit = _MAX_TERM_DOC_FREQUENCY * len(claim_tokens)
    return {
        token
        for token in _tokens(name)
        if sum(token in tokens for tokens in claim_tokens) < limit
    }


def _distinctive_claim_terms(claim_texts: list[str]) -> set[str]:
    """Terms rare enough across THIS run's claims to be evidence of a link.

    The corpus-frequency counterpart of _distinctive_name_tokens, over prose
    rather than names. A term absent from every claim is excluded by
    construction: it can never be part of an overlap, so counting it as signal
    an entity possesses would only raise the bar with nothing behind it - which
    is exactly what a raw funding figure like '1020000000' would otherwise do.
    """
    if not claim_texts:
        return set()
    limit = _MAX_TERM_DOC_FREQUENCY * len(claim_texts)
    counts: dict[str, int] = {}
    for text in claim_texts:
        for word in _significant_words(text):
            counts[word] = counts.get(word, 0) + 1
    return {word for word, count in counts.items() if count < limit}


def _check_company_is_named(
    company_name: str,
    cited_texts: list[str],
    all_claim_texts: list[str],
    label: str,
    issues: list[CitationIssue],
) -> None:
    """Require at least one cited claim to actually name the company.

    This is the check that does the real work on company profiles.
    _check_weak_support is a vocabulary-overlap test, and inside a
    single-sector corpus nearly every claim shares vocabulary with nearly every
    entity: measured across the five saved runs, an arbitrary claim paired with
    an arbitrary company profile clears that heuristic 87% of the time, so it
    cannot tell a profile's own evidence from a competitor's. Naming the
    company is the one thing a profile's evidence has to do, and unlike
    vocabulary overlap it is a necessary condition rather than a sufficient
    one.

    Evaluated across the citation set, matching _check_weak_support: a profile
    cites several claims covering different attributes and they need not all
    name the company, but at least one must.
    """
    distinctive = _distinctive_name_tokens(company_name, all_claim_texts)
    if not distinctive or not cited_texts:
        return
    if any(distinctive & _tokens(text) for text in cited_texts):
        return
    issues.append(CitationIssue(
        claim_or_entity=company_name,
        problem=(
            f"{label} cites {len(cited_texts)} claim(s), none of which mention "
            f"the company by name - the profile may rest on another company's "
            f"evidence."
        ),
    ))


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
    distinctive_terms: set[str],
    max_required: int = _MAX_REQUIRED_TERMS,
) -> None:
    """Flag an entity when none of its cited claims plausibly relate to it.

    Two things make this more than a word-overlap test.

    **Only distinctive terms count.** Every claim in a run is about one sector,
    so 'energy', 'power' and 'reactor' are shared by nearly everything and
    prove nothing. Counting any shared word at all, an arbitrary claim paired
    with an arbitrary entity cleared this check 74% of the time across the five
    saved runs - it was a topic detector, not a relevance test. Restricting to
    terms rare within the run's own claims is what gives overlap meaning.

    **The bar scales with the signal available.** Requiring two shared
    distinctive terms roughly halves the false-pass rate again, but it is only
    fair to an entity that HAS two to offer. A funding event's citing text is a
    few structured fields, so it is held to whatever it has. Hence
    min(max_required, len(usable)) rather than a fixed threshold - measured, a
    fixed bar of two flags correct funding events while a bar of one lets
    everything through.

    Evaluated across the whole citation set, not per claim. An entity usually
    cites several claims that each support a different part of it - a company
    profile might cite one claim for the funding round and another for the
    technical differentiation - so demanding that EVERY cited claim relate to
    the citing text flags correct output constantly. A live end-to-end run
    escalated to human review for exactly this: a CorPower Ocean profile whose
    differentiation described cost reductions, citing a perfectly good claim
    about its Series B.

    A citation set where nothing relates to the citing text is still the
    egregious case this heuristic was written to catch, and it is still caught.
    """
    if not claim_texts:
        return

    usable = _significant_words(citing_text) & distinctive_terms
    if usable:
        required = min(max_required, len(usable))
        related = any(
            len(usable & _significant_words(claim_text)) >= required
            for claim_text in claim_texts
        )
    else:
        # Nothing distinctive to test with - either a very small corpus, or an
        # entity described entirely in sector vocabulary. Fall back to the
        # unfiltered bar rather than flag: a false positive here escalates a
        # correct briefing to a human, which is the expensive failure.
        required = _MIN_OVERLAP_WORDS
        related = any(
            _overlap_count(citing_text, claim_text) >= _MIN_OVERLAP_WORDS
            for claim_text in claim_texts
        )

    if related:
        return
    issues.append(CitationIssue(
        claim_or_entity=citing_text,
        problem=(
            f"{label} cites {len(claim_texts)} claim(s), none with enough "
            f"shared terminology - needs {required} distinctive term(s) in "
            f"common (e.g. '{claim_texts[0][:80]}...') - possible spurious "
            f"citation."
        ),
    ))


def check_citations(analysis: AnalysisResult) -> FactCheckResult:
    issues: list[CitationIssue] = []
    verified = 0
    max_index = len(analysis.all_claims) - 1
    all_claim_texts = [claim.claim for claim in analysis.all_claims]
    distinctive_terms = _distinctive_claim_terms(all_claim_texts)

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
        _check_weak_support(
            rec.text, cited, "Recommendation", issues, distinctive_terms,
        )

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
        # prose is about some other attribute entirely. That makes the name
        # SUFFICIENT here; _check_company_is_named makes it NECESSARY, which is
        # where the actual discrimination comes from.
        _check_weak_support(
            f"{company.name} {company.differentiation}", cited, label, issues,
            distinctive_terms,
        )
        _check_company_is_named(
            company.name, cited, all_claim_texts, label, issues,
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
            # is genuinely per-claim. Held to one shared term: the citing text
            # is structured fields, and neither a raw float nor a stage enum
            # appears in prose the way a claim writes it ('$1.02 billion',
            # not '1020000000.0').
            cited_claim = analysis.all_claims[idx].claim
            _check_weak_support(
                event_text, [cited_claim], label, issues, distinctive_terms,
                max_required=1,
            )
            # The meaningful check for a funding event, same as for a profile:
            # the claim behind it has to be about this company.
            _check_company_is_named(
                event.company_name, [cited_claim], all_claim_texts, label, issues,
            )

    # -- 4. Tensions ---------------------------------------------------
    # The section that had no verification at all before it was given a
    # structure. Everything below is decidable in plain Python precisely
    # because the citations are split by side.
    for tension in analysis.tensions_or_conflicts:
        label = f"Tension '{tension.statement[:50]}'"
        cited = []
        for indices, expected_type in (
            (tension.internal_claim_indices, SourceType.INTERNAL),
            (tension.external_claim_indices, SourceType.EXTERNAL),
        ):
            if not indices:
                issues.append(CitationIssue(
                    claim_or_entity=tension.statement,
                    problem=(
                        f"{label} cites no {expected_type.value} claims. A "
                        f"disagreement between internal and external sources "
                        f"needs at least one claim from each side."
                    ),
                ))
                continue
            for idx in indices:
                if not _validate_index(idx, max_index, label, tension.statement, issues):
                    continue
                verified += 1
                claim = analysis.all_claims[idx]
                cited.append(claim.claim)
                if claim.source_type is not expected_type:
                    issues.append(CitationIssue(
                        claim_or_entity=tension.statement,
                        problem=(
                            f"{label} lists claim {idx} as "
                            f"{expected_type.value}, but that claim is "
                            f"{claim.source_type.value} "
                            f"('{claim.claim[:60]}...'). A tension built from "
                            f"claims on one side is not a disagreement."
                        ),
                    ))
        _check_weak_support(tension.statement, cited, label, issues, distinctive_terms)

    return FactCheckResult(
        passed=len(issues) == 0,
        verified_count=verified,
        issues=issues,
    )