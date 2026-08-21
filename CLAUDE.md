# CLAUDE.md

This file orients Claude Code on a project already in progress. Read it first,
then skim `AGENTS.md` (a general CrewAI reference) before writing CrewAI code.
The goal is to pick up building exactly where the last session left off.

## What this project is

A take-home technical assignment for a **Forward Deployed Engineer** role at
**CrewAI** (the agentic-AI-workflow company). The assignment: build a "Deep
Research Agent" that gathers both external (public internet) and internal
(mocked, static-file) information on a topic and produces an executive-ready
report.

**The chosen framing:** a venture capital firm called **Northbridge
Ventures**, specializing in emerging technology investments, wants a sector
research tool. The live demo topic area is **emerging energy technologies** -
small modular reactors (SMRs), molten salt reactors, enhanced geothermal
systems, and wave/marine energy. The tool researches a given sector and
produces an investment-committee-ready briefing: what's changing, who's
leading vs. entering, where capital is flowing, and a concrete investment
recommendation.

This is a graded take-home, not a real product - but it should be built with
real production instincts, since that's what an FDE interview is actually
screening for.

**Missing context, do not go looking for it:** earlier versions of this file
referenced `docs/01-take-home-brief.md`, `docs/02-architecture.md`, and
`docs/03-crewai-jsonc-reference.md`. No `docs/` directory was ever transferred
into this repo - that context was lost in the handoff from the original
claude.ai planning conversation. The verified JSONC mechanics that `docs/03`
was supposed to hold are reproduced in this file instead (see "CrewAI JSONC
config" below). Ask the user before recreating the other two.

## Package name

**`crewai_exec_deep_research_agent`** - not `deep_research_agent`. If you ever
see `deep_research_agent` (no prefix) referenced anywhere, that's stale - fix
it to match.

## Core design principles (apply these to everything you build next)

1. **Structured data at every crew boundary.** No free text crosses a
   crew/Flow seam unchecked - everything is a typed Pydantic model
   (`src/crewai_exec_deep_research_agent/models.py`). This is what makes
   fact-checking, deterministic formatting, and reliable routing possible.
2. **Deterministic guardrails, not agentic ones, wherever possible.** The
   thing that decides whether a report is trustworthy should not be the same
   kind of process (an LLM) that could hallucinate. The citation-check gate
   and both Research Crew task guardrails are plain Python. Apply this
   instinct to new gates too.
3. **Mocks are swappable behind stable interfaces.** `internal_kb_tool` and
   `web_search_tool` both stand in for real integrations (a real client's
   document store; a real search API). Keep the CrewAI-facing tool interface
   stable so swapping the internals later doesn't touch agent/task configs.
4. **Never let a tool or a stage fail silently.** Every tool failure mode
   returns an explicit string telling the agent what happened and instructing
   it not to fabricate. The same applies upward: `ResearchCrew.run()` raises
   rather than returning empty claims when a task's output didn't parse.
   A silently-empty research stage looks exactly like a topic nobody has
   written about, which is the most dangerous possible failure here.
5. **Test what's actually testable without an LLM.** Deterministic logic
   (citation checks, retrieval scoring, tool error handling, guardrails, the
   merge, and crew concurrency) gets real unit tests, run for real against
   actual fixture data. Don't skip actually running `pytest`.
6. **Schema mirrors report structure.** `AnalysisResult`'s fields map
   one-to-one onto `knowledge/style_guide.md`'s report sections, so the Report
   Crew's Formatter agent is closer to templating structured data than freely
   generating prose from scratch.

## Current status

**Built and verified (tests run and passing - 56 total):**
- `models.py` - full Pydantic schema for the pipeline
- `flows/deep_research_flow.py` - the orchestrating Flow (intake → research →
  analysis → fact-check gate → report, with bounded retry + human escalation).
  Its research step is wired to the real crew; analysis and report steps still
  import crews that don't exist yet.
- `tools/citation_check_tool.py` + 12 tests - deterministic fact-check gate
- `tools/internal_kb_tool.py` + 15 tests - keyword retrieval over mock docs
- `tools/web_search_tool.py` + 10 tests - Serper.dev wrapper. Network calls are
  mocked in tests, but the response shape **has** now been confirmed against
  the live API.
- **Research Crew - complete and verified against a live run.** See its own
  section below.
- `knowledge/internal_docs/*.md` - 5 mock internal documents covering all four
  demo technologies plus two cross-cutting docs
- `knowledge/style_guide.md` + `knowledge/prior_exec_report_sample.md` - house
  style reference for the Report Crew. Note both live in `knowledge/` directly;
  only real internal evidence belongs in `knowledge/internal_docs/`, since
  everything in that folder is retrievable by the internal KB tool and citable
  as a source.
- `sample_runs/research_enhanced_geothermal.json` - real output from a live
  Research Crew run, useful as a fixture for building the Analysis Crew without
  paying for a research run every iteration.

**Not started yet - this is where to resume:**
- Analysis Crew (agents/tasks). Use JSONC for consistency with the Research
  Crew; this was previously listed as an open question and is now settled.
- Report Crew (Formatter + Style Reviewer agents)
- Wiring the analysis and report crews into `flows/deep_research_flow.py`
  (the research step is already wired; the other two still reference modules
  that don't exist)
- `main.py` - still the untouched `crewai create` template. It defines a
  `ContentFlow` importing a nonexistent `content_crew`, and `pyproject.toml`'s
  scripts point at it, so `crewai run` does not work yet.
- `README.md` - still the `{{crew_name}}` template
- `sample_runs/topics.json` (a small set of energy-tech topics to run
  end-to-end once the pipeline works)

## The Research Crew (reference implementation - copy its patterns)

Lives in `src/crewai_exec_deep_research_agent/crews/research_crew/`. It is
**one stage implemented as two single-agent crews**, run concurrently in
threads by `ResearchCrew.run()`, which merges both results into one
`ResearchFindings`.

```
research_crew/
  research_crew.py            # concurrent kickoff + merge; the Flow's entry point
  research_guardrails.py      # shared deterministic guardrails
  external_research/
    crew.jsonc                # external_research_task
    agents/web_researcher.jsonc
    external_refs.py          # project-local re-export (see below)
  internal_research/
    crew.jsonc                # internal_research_task
    agents/internal_researcher.jsonc
    internal_refs.py
```

**Why two crews instead of one crew with two parallel tasks:** CrewAI won't
allow it. `Crew.validate_end_with_at_most_one_async_task` rejects a crew ending
in more than one async task, and pairing `async → sync` doesn't help either,
because `_run_sequential_process` drains pending futures *before* starting the
sync task. Two independent tasks genuinely cannot run concurrently inside one
crew. The parallelism is worth the extra directory: the internal and external
views must be gathered without either seeing the other, or the final report's
"Where Sources Disagree" section measures contamination instead of real
disagreement.

**Why the merge is plain Python, not a third agent task:** it's list
concatenation over already-validated `ClaimList` objects. An LLM would add
cost and latency and a fresh chance to silently reword or drop claims.

Use threads (not `asyncio.run`) for any similar fan-out - these run inside a
Flow step, which may already own a running event loop.

## CrewAI JSONC config - verified mechanics

The user asked for JSONC (`crew.jsonc` + `agents/<name>.jsonc`) rather than
classic YAML/`@CrewBase`. `AGENTS.md` does not document JSONC at all, so the
following was read directly out of `crewai` 1.15.17's
`crewai/project/json_loader.py` and `crew_loader.py`. It is ground truth,
confirmed by a live run - but re-verify against the installed version if
CrewAI is ever upgraded.

- **Allowed keys** are exactly `Agent`/`Task`/`Crew` model fields minus runtime
  fields. Unknown keys are hard errors, so a typo fails loudly rather than
  being ignored. An agent may also carry a nested `settings: {...}` object,
  which is merged into the agent's kwargs.
- **`inputs`** on a crew is allowed, and is returned by `load_crew` as default
  kickoff inputs rather than being passed to `Crew(...)`.
- **Tools** use `"module.path:ClassName"` import refs pointing straight at the
  real tool class, e.g.
  `"crewai_exec_deep_research_agent.tools.web_search_tool:WebSearchTool"`.
  There's no project-root restriction on these, and no shim file is needed.
  (`custom:<name>` shims in a per-crew `tools/` directory also work, but are
  strictly more files for the same result.)
- **`{"python": "..."}` refs** - used for `output_pydantic`, `guardrail`, and
  similar - are a *different, restricted* resolver. `_project_module_file()`
  requires the target module to live **inside the directory holding
  crew.jsonc**, and raises "Python references in JSON configs must point to
  modules inside the project root" otherwise. This is why each crew directory
  has its own small `*_refs.py` re-exporting the real definitions from the
  package. Give those modules crew-specific names - the loader imports them as
  top-level modules, so two files both named `refs.py` would collide in
  `sys.modules`.
- **`llm`** accepts a config dict, not just a model string:
  `{"model": "anthropic/claude-sonnet-5", "max_tokens": 16000}`. Use the dict
  form. See known gap #1 for why `max_tokens` is not optional here.
- The `anthropic` extra is required for `anthropic/...` model strings -
  `pyproject.toml` depends on `crewai[tools,anthropic]`. Without it, agent
  construction fails at load time.
- Validate a config without spending an LLM call:
  `load_crew("path/to/crew.jsonc")` builds the whole crew - agents, tools,
  refs, guardrails - without kicking anything off. Always do this before a
  live run.

## Known gaps / things to double-check before trusting this code

1. **Agent `max_tokens` must be set explicitly.** CrewAI's Anthropic provider
   defaults to **4096**, which a long structured output exceeds. Observed
   live: the web researcher's JSON was truncated mid-field and all 31 of its
   claims were lost. Both research agents now set 16000. Any new agent
   returning a large `output_pydantic` payload needs the same treatment.
2. **Never give a list field on an `output_pydantic` model a default.**
   `ClaimList.claims` is deliberately required. When it had
   `default_factory=list`, a truncated response still validated into an empty
   `ClaimList`, making "ran out of tokens mid-JSON" indistinguishable from
   "found nothing" - the failure silently swallowed real findings.
   `tests/test_research_guardrails.py` locks this in.
3. **`internal_kb_tool.py`'s keyword-overlap retrieval returns loosely related
   chunks whenever there's *any* shared vocabulary**, even for queries the docs
   don't really answer. This is a known, accepted limitation - the internal
   researcher's task description explicitly instructs the agent to judge
   relevance itself rather than trusting the tool.
4. **`citation_check_tool.py`'s weak-support heuristic is deliberately
   conservative** (low overlap threshold) to avoid false-positiving on
   legitimate, well-paraphrased citations. It will miss some real problems.
   This tradeoff was made on purpose - don't "fix" it by raising the threshold
   without re-running the full test suite, since
   `test_paraphrased_but_related_citation_is_not_flagged_as_weak` exists
   specifically to catch that regression.
5. **The external researcher over-produces.** Its task asks for 8-15 claims;
   a live run returned 31. All were well-sourced, so this is a prompt-adherence
   gap rather than a correctness bug, but tighten it if the Analysis Crew
   struggles with the volume.
6. **Internal document filenames are part of the deliverable.** They're cited
   verbatim in the report's Sources appendix, so they follow a consistent
   `internal_*` convention and are spelled correctly. Three tests assert
   specific filenames; renaming a doc means updating
   `tests/test_internal_kb_tool.py`.

## How to resume a session on this project

1. Read this file, then the documents in `knowledge/` (`style_guide.md` first -
   it defines the report the whole pipeline is building toward).
2. Build the Analysis Crew next, following the Research Crew's patterns above.
   `sample_runs/research_enhanced_geothermal.json` is real `ResearchFindings`
   output you can develop against without paying for research runs.
3. Run `load_crew()` on any new JSONC config before a live run - it catches
   config errors for free.
4. Run `pytest` after every change that touches existing tested code. The
   project's convention throughout has been "don't claim something works, run
   it and show the output."
