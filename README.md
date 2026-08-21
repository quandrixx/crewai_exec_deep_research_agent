# Deep Research Agent — Northbridge Ventures

A multi-agent research pipeline built with [CrewAI](https://crewai.com). Give it
a technology sector; it researches the public web and the firm's own internal
documents in parallel, reconciles what they say, verifies every citation, and
produces an investment-committee-ready briefing.

```bash
uv run kickoff "molten salt reactors"
```

```
TOPIC: molten salt reactors
  research   : 24 external + 8 internal claims
  analysis   : 5 shifts, 7 companies, 4 funding events, 3 tensions, 4 recommendations
  fact-check : PASSED (42 citations verified, 0 issues, 0 revision round(s))

  written:
    output/molten_salt_reactors/research.json
    output/molten_salt_reactors/analysis.json
    output/molten_salt_reactors/fact_check.json
    output/molten_salt_reactors/report.json
    output/molten_salt_reactors/report.md

  Should Northbridge Increase Sourcing Activity in Molten Salt Reactors?
  1049 words, 27 sources, fact-check status: passed
```

That is a verbatim run, not an illustration. Its output is checked in:
[`sample_runs/report_molten_salt_reactors.md`](sample_runs/report_molten_salt_reactors.md)
— alongside briefings on
[small modular reactors](sample_runs/report_small_modular_reactors.md) and
[wave and tidal energy](sample_runs/report_wave_tidal_energy.md), plus the
intermediate `research_*.json` and `analysis_*.json` each was built from.

---

## The scenario

**Northbridge Ventures** is a VC firm investing in emerging energy technology.
Partners need to answer one question per sector: *is there a thesis here, is the
timing right, and where would we put capital if the answer is yes?*

The hard part isn't summarizing the news. It's that the firm **already has
opinions**, written down in old memos, and those opinions go stale. The most
valuable thing this tool produces is the moment where the firm's own prior
reasoning collides with current evidence — from a real run:

> Northbridge passed **eighteen months ago** on regulatory-timeline risk,
> citing unrealistic NRC licensing assumptions. Executive Order 14300
> established binding **eighteen-month** deadlines, and **Developer X**
> received favorable NRC pre-application engagement — directly addressing the
> original pass thesis.

That finding is only possible because the internal and external halves are
researched **independently**, without either seeing the other. Otherwise you're
measuring contamination, not disagreement.

---

## Quickstart

Requires Python 3.10–3.13 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                      # installs deps, including dev group
cp .env.example .env         # then add your keys
```

`.env` needs:

| Variable | Required | Purpose |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | yes | every agent in the pipeline |
| `SERPER_API_KEY` | recommended | external web search ([serper.dev](https://serper.dev), free tier) |

Without `SERPER_API_KEY` the run still completes using internal sources alone,
and warns you that it did. Without `ANTHROPIC_API_KEY` it stops immediately
rather than failing a minute in.

```bash
crewai run                                    # default topic
uv run kickoff "enhanced geothermal systems"  # any topic
uv run kickoff --list-topics                  # suggested demos
uv run plot                                   # flow diagram -> output/flow/
uv run pytest                                 # 170 tests, no API calls
```

Output lands in `output/<topic>/`: `research.json`, `analysis.json`,
`fact_check.json`, `report.json`, `report.md`.

**Exit codes:** `0` report produced · `2` escalated for human review (the safety
machinery working, but no deliverable) · `1` crashed.

---

## How it works

```
                    ┌─────────────────────────────────────┐
  topic ──▶ Research Crew                                 │
            ├── Web Researcher      ─┐  run concurrently  │
            └── Internal Researcher ─┘                    │
                    │  ResearchFindings (typed claims)    │
                    ▼                                     │
            Analysis Crew                                 │
            ├── Sector Analyst        (organizes evidence)│
            └── Investment Strategist (takes a position)  │
                    │  AnalysisResult                     │
                    ▼                                     │
            ┌───────────────────┐                         │
            │ Fact-check gate   │  plain Python, no LLM   │
            └───────────────────┘                         │
                 pass │ fail                              │
                      │  └──▶ revise once ──▶ still fail ─┴─▶ escalate,
                      ▼                                       report withheld
            Report Crew
            ├── Briefing Writer
            └── Style Reviewer
                    │  FinalReport
                    ▼
              report.md
```

A CrewAI **Flow** orchestrates three **Crews**. Every boundary between them is a
typed Pydantic model ([`models.py`](src/crewai_exec_deep_research_agent/models.py)) —
no free text crosses a seam unchecked, which is what makes the fact-checking and
deterministic formatting possible.

### The fact-check gate

Every claim gathered gets an index. Every company profile, funding event, and
recommendation must cite the indices of the claims supporting it. The gate is
[plain Python](src/crewai_exec_deep_research_agent/tools/citation_check_tool.py) —
deliberately **not** an agent, because the thing deciding whether a report is
trustworthy shouldn't be the same kind of process that could hallucinate.

It checks that cited indices resolve to real claims, and flags citation sets
where nothing shares vocabulary with the thing citing them. On failure the Flow
feeds the specific problems back for one bounded revision; if that fails too, it
**withholds the report** and escalates with the failed citations named.

### What is deliberately not done by an LLM

This is the through-line of the whole design. If Python can guarantee it, an
agent doesn't get to touch it:

| Thing | Why |
| --- | --- |
| Citation verification | An LLM checking an LLM's citations is theatre |
| Sources appendix | Built from the verified claim list, so every listed source provably backed a claim |
| Funding table | Rendered from structured data — a model asked to reproduce one instead rewrote it, reporting a round in the wrong currency |
| Merging parallel results | List concatenation; an LLM adds cost, latency, and a chance to silently drop a claim |
| `fact_check_status` | Read from the gate, not from an agent's opinion of its own work |

Agents write prose and exercise judgment. Everything else is code.

---

## What's mocked

Per the brief, internal sources are static files:
[`knowledge/internal_docs/`](knowledge/internal_docs/) holds five documents — an
advanced-nuclear investment thesis, a geothermal diligence memo, wave-energy
scouting notes, a portfolio check-in, and a quarterly sector scan.

Both retrieval tools sit behind stable CrewAI tool interfaces, so swapping the
mock for a real backend doesn't touch a single agent or task config:

- [`internal_kb_tool.py`](src/crewai_exec_deep_research_agent/tools/internal_kb_tool.py)
  — keyword retrieval over the mock corpus. Replace the internals with
  Confluence, SharePoint, or a vector store over deal memos.
- [`web_search_tool.py`](src/crewai_exec_deep_research_agent/tools/web_search_tool.py)
  — Serper.dev. Replace with Tavily, Bing, or anything else.

Every tool failure returns an explicit message telling the agent what broke and
instructing it not to fabricate — never a silent empty result. A missing
knowledge base and a genuine "the firm has no prior work here" produce
*different* messages, because conflating them lets a broken deployment
masquerade as a research finding.

---

## Testing

```bash
uv run pytest        # 170 tests, ~5s, no API calls
```

Everything deterministic is tested for real: citation checking, retrieval
scoring, tool error handling, every guardrail, the merges, artifact
persistence, flow routing, and — using stubbed LLMs against the real crew
config — that the two research agents genuinely overlap in time.

Several tests are regression locks on failures found only in live runs, and each
says so in its docstring. A few examples:

- A truncated response once validated into an *empty* result because a list
  field had a default, silently discarding 15 real claims.
- The citation gate once escalated a correct briefing because it demanded every
  cited claim share vocabulary with the citing text, rather than at least one.
- `FundingStage` stopped at `series_b` until a real X-energy Series D broke it.

---

## Configuration

Crews are defined in CrewAI's JSONC format — `crew.jsonc` plus one file per
agent — so prompts and wiring are readable as data:

```
crews/research_crew/
  crew.jsonc                        # 2 async research tasks + 1 sync barrier
  agents/web_researcher.jsonc
  agents/internal_researcher.jsonc
  research_guardrails.py            # deterministic output validation
  research_crew.py                  # loads the crew, merges the results
```

Two CrewAI mechanics worth knowing if you extend this, both verified against the
1.15.17 source rather than docs:

- A crew **cannot end with two async tasks**. The Research Crew's two
  researchers run concurrently only because a synchronous barrier task follows
  them; both are submitted as futures before the barrier is reached.
- `{"python": ...}` references in JSONC must resolve to a module **inside the
  crew's own directory**, which is why each crew has a small `*_refs.py`
  re-exporting the real definitions.

[`CLAUDE.md`](CLAUDE.md) documents these and every other sharp edge found while
building, including the measured reason the analysis and report agents run a
different model than the research agents.

---

## Known limitations

- **Retrieval is keyword-overlap, not semantic.** It returns loosely related
  passages whenever vocabulary overlaps, so the internal researcher is
  explicitly instructed to judge relevance itself. A real deployment would put
  a vector store behind the same tool interface.
- **The weak-support heuristic is conservative by design** and will miss subtle
  mis-citations. Raising its threshold trades false negatives for false
  positives, and a false positive withholds a good report.
- **The external researcher over-produces**, returning ~20 claims where the task
  asks for 8–15. All are well-sourced; it's a prompt-adherence gap.
- **One revision round, then escalation.** Bounded deliberately — a pipeline
  that retries forever on evidence that genuinely can't support the claims is
  worse than one that asks a human.
