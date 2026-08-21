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
   kind of process (an LLM) that could hallucinate. The citation-check gate and
   every crew's task guardrails are plain Python. Note the deliberate split of
   labor: a guardrail sees only the TaskOutput, so it checks what's decidable
   from the output alone (structure, non-emptiness, non-negative indices), while
   `citation_check_tool` runs in the Flow with the claim list in hand and
   verifies the indices actually resolve. Guardrail failures are cheap (one task
   retries); gate failures are expensive (the crew re-runs). Apply this
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

**Built and verified (tests run and passing - 141 total):**
- `models.py` - full Pydantic schema for the pipeline
- `flows/deep_research_flow.py` - the orchestrating Flow (intake → research →
  analysis → fact-check gate → report, with bounded retry + human escalation).
- `tools/citation_check_tool.py` + 12 tests - deterministic fact-check gate
- `tools/internal_kb_tool.py` + 15 tests - keyword retrieval over mock docs
- `tools/web_search_tool.py` + 10 tests - Serper.dev wrapper. Network calls are
  mocked in tests, but the response shape **has** now been confirmed against
  the live API.
- **Research Crew - complete and verified against a live run.** See its own
  section below.
- **Analysis Crew - complete and verified against a live run**, including the
  Flow's fact-check gate passing on real output (42 citations verified). See
  its own section below.
- **Report Crew - complete and verified against a live run.** See its own
  section below. `sample_runs/report_small_modular_reactors.md` is a finished
  briefing produced end-to-end from real research.
- `flows/deep_research_flow.py` is fully wired: all three crews, the fact-check
  gate, the bounded revision loop, and human escalation. Verified structurally
  in `tests/test_flow_wiring.py`; the full flow has NOT yet been run end-to-end
  in one go (each stage has been run live from the previous stage's saved
  output).
- `knowledge/internal_docs/*.md` - 5 mock internal documents covering all four
  demo technologies plus two cross-cutting docs
- `knowledge/style_guide.md` + `knowledge/prior_exec_report_sample.md` - house
  style reference for the Report Crew. Note both live in `knowledge/` directly;
  only real internal evidence belongs in `knowledge/internal_docs/`, since
  everything in that folder is retrievable by the internal KB tool and citable
  as a source.
- `sample_runs/*.json` - real output from live runs: `research_*.json` are
  `ResearchFindings`, `analysis_*.json` is a full `AnalysisResult`. Use these
  as fixtures when building the Report Crew instead of paying for an upstream
  run every iteration - `AnalysisResult.model_validate_json(...)` loads one
  directly.

**Not started yet - this is where to resume:**
- One full end-to-end `DeepResearchFlow().kickoff()` run on a fresh topic, to
  confirm the stages hand off correctly in a single process.
- `main.py` - still the untouched `crewai create` template. It defines a
  `ContentFlow` importing a nonexistent `content_crew`, and `pyproject.toml`'s
  scripts point at it, so `crewai run` does not work yet.
- `README.md` - still the `{{crew_name}}` template
- `sample_runs/topics.json` (a small set of energy-tech topics to run
  end-to-end once the pipeline works)

## The Research Crew (reference implementation - copy its patterns)

Lives in `src/crewai_exec_deep_research_agent/crews/research_crew/`. One crew,
two researchers running concurrently, plus a synchronization barrier task.
`ResearchCrew.run()` kicks it off and merges the two claim lists into one
`ResearchFindings`.

```
research_crew/
  crew.jsonc                  # 2 async research tasks + 1 sync barrier task
  agents/web_researcher.jsonc
  agents/internal_researcher.jsonc
  agents/research_coordinator.jsonc   # owns the barrier task, nothing else
  research_refs.py            # project-local re-exports for {"python": ...} refs
  research_guardrails.py      # deterministic per-task validation
  research_crew.py            # kickoff + merge; the Flow's entry point
```

**How the parallelism works, and why the third task exists.**
`Crew.validate_end_with_at_most_one_async_task` rejects a crew whose *trailing*
run of tasks contains more than one async task, so two async tasks alone will
not construct. A trailing synchronous task fixes that at no cost to
parallelism: `_run_sequential_process` submits both async tasks as futures
before it ever reaches the sync task, which then just drains work already
running. Verified with stubbed LLMs -
`tests/test_research_crew_merge.py::test_the_two_research_tasks_actually_overlap_in_time`
asserts the two research tasks genuinely overlap in wall-clock time, and it
runs without any network calls.

The barrier's own cost is one small LLM call per run (200 max_tokens, no
tools, minimal backstory). A `ConditionalTask` whose condition returns false is
skipped without any LLM call while still forcing the drain, so it's a free
alternative if that call ever matters - at the cost of being much less obvious
to a reader.

Note the ordering constraint this creates: **any new task added to this crew
must go after the barrier, or the barrier stops being last and the crew stops
validating.** The structure is pinned by
`test_real_config_has_two_async_tasks_followed_by_a_sync_barrier`.

**Why the two research tasks have no `context` link to each other:** they must
be gathered without either seeing the other, or the final report's "Where
Sources Disagree" section measures contamination instead of real disagreement.
A context link would also serialize them.

**Why the merge is plain Python, not the barrier's job:** it's list
concatenation over already-validated `ClaimList` objects. An LLM would add
cost and latency and a fresh chance to silently reword or drop claims. The
barrier is explicitly told its output is a checkpoint marker that nothing
downstream reads.

## The Analysis Crew

`crews/analysis_crew/`. Turns `ResearchFindings` into an `AnalysisResult`.
Two sequential tasks - a Sector Analyst organizes the evidence, then an
Investment Strategist reads that and commits to a position. Sequential, not
parallel: the strategist genuinely depends on the analyst, so there's nothing
to gain from async and no barrier task needed.

**The claim-index contract - the most important thing in this crew.** Every
citation in the finished report is an integer offset into
`AnalysisResult.all_claims`, and the agents pick those integers by reading a
numbered list rendered into their prompt. `analysis_crew.py` defines the
ordering once in `_build_claim_index()` (external claims, then internal);
`_format_claims()` renders that same list; the merge puts that same list into
`all_claims`. If the rendering and `all_claims` ever drift apart, every
recommendation silently cites the wrong evidence *while still passing the
fact-check gate*, because the indices all still resolve. Change the ordering in
`_build_claim_index()` alone.

The agents never reproduce claims - only indices. Asking an LLM to echo 28
claims verbatim is pure cost and risk: reworded text breaks every index, and a
large payload is what got truncated in a live Research Crew run.

**Both agents are toolless on purpose.** Every fact in the report must trace to
a claim the Research Crew gathered. An analyst that could search the web would
introduce evidence no citation index can point at. This has a surprising
consequence for model choice - see known gap #3.

## The Report Crew

`crews/report_crew/`. Renders a fact-checked `AnalysisResult` into a
`FinalReport`. Two sequential tasks: an Investment Briefing Writer produces the
draft, then a Style Reviewer corrects it against `knowledge/style_guide.md`.

**Three things are deliberately kept away from the LLM**, all in
`report_crew.py`:

  - **The sources appendix**, built from `AnalysisResult.all_claims`. This is
    the single most important one. It's the only place a fabrication would slip
    past every other check in the pipeline - the fact-check gate verifies claim
    *indices*, not source strings, and nothing downstream re-reads the appendix.
    Deriving it from the verified claim list makes "every listed source actually
    backed a claim" true by construction.
  - **The funding table**, rendered from structured `FundingEvent` data, exactly
    as models.py always intended. Passed into the prompt pre-rendered, with the
    writer told to paste it verbatim.
  - **`fact_check_status`**, set by the Flow from the gate's result - not by an
    agent's opinion of its own work. A report that needed a revision round is
    marked `passed_with_flags`, and `render_markdown()` puts that warning on the
    face of the document.

**The style guide is unusually checkable**, so `report_guardrails.py` enforces
the mechanical half (required sections, order, no hand-written Sources section,
length band, banned promotional phrases) and the Style Reviewer agent spends its
attention on judgment - vagueness, decisiveness, whether each recommendation
states its risk.

One tuning note worth keeping: the length band matters more than it looks. With
a loose ceiling, a live run shipped at **1151 words** against the guide's 600-900
target while the reviewer reported nothing to fix. Tightening the ceiling to
1100 and making length an explicit required edit in the reviewer's prompt
brought the next run to **850 words**, with the guardrail bouncing one draft
along the way.

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
  modules inside the project root" otherwise. This is why the crew directory
  has a small `research_refs.py` re-exporting the real definitions from the
  package. Give these modules crew-specific names rather than something generic
  like `refs.py` - the loader imports them as top-level modules, so two crews
  using the same filename would collide in `sys.modules`.
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
- **Long prompts have to be single-line strings with `\n` escapes.** JSON has
  no multi-line string literal, and CrewAI passes the value straight to
  `Task`/`Agent`, where Pydantic rejects an array of lines. A custom loader
  that joins string arrays was built and then deliberately reverted - if this
  comes up again, it works, but it means the project's configs can no longer
  be read by CrewAI's own `load_crew()`, which is the cost that decided it.

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
3. **Toolless agents + `output_pydantic` + sonnet-5 = broken structured
   output.** This one cost real debugging time. When an agent has NO tools,
   CrewAI passes the task's `output_pydantic` to the model as a `response_model`
   (`crew_agent_executor.py`: `None if self.original_tools else
   self.response_model`). It then picks a strategy from a hardcoded allowlist of
   Claude **4.5** names (`_supports_native_structured_outputs`). A 4.5 model gets
   Anthropic's native `json_schema` format, enforced server-side. Anything else -
   including `claude-sonnet-5` - falls back to a tool-based approach where the
   model returns its fields nested under `"parameters"`, and Pydantic validation
   fails with "Field required" for every top-level field.

   Measured on the real 28-claim analysis workload: **sonnet-5 failed 3 runs out
   of 3; sonnet-4-5 succeeded.** It does NOT reproduce on small prompts, so a
   toy repro will mislead you. Hence the Analysis Crew's agents run
   `claude-sonnet-4-5` while the Research Crew stays on `claude-sonnet-5` - the
   research agents have tools, so they never take this path at all. Revisit when
   CrewAI's allowlist learns about sonnet-5.

4. **A Flow listener label must never equal a handler's own name.** CrewAI
   rejects `@listen("revise_analysis")` on a method also called
   `revise_analysis` - it reads as a listener triggered by its own completion,
   an infinite loop - and it raises at *construction* time, so nothing catches
   it until something actually instantiates the flow. The skeleton shipped with
   this bug in three places and it stayed invisible for most of the project.
   Routing labels are now named for the decision (`needs_revision`,
   `ready_for_report`, `needs_human_review`) and handlers for the work.
   `tests/test_flow_wiring.py` constructs the flow so a regression fails loudly.

   Related: a `@router` only re-evaluates when its source method runs as part of
   the flow. Calling that method directly from a handler updates state without
   re-triggering routing, so the revision loop uses a second `@router` on
   `revise_analysis` rather than calling `fact_check()` by hand.

5. **`internal_kb_tool.py`'s keyword-overlap retrieval returns loosely related
   chunks whenever there's *any* shared vocabulary**, even for queries the docs
   don't really answer. This is a known, accepted limitation - the internal
   researcher's task description explicitly instructs the agent to judge
   relevance itself rather than trusting the tool.
6. **`citation_check_tool.py`'s weak-support heuristic is deliberately
   conservative** (low overlap threshold) to avoid false-positiving on
   legitimate, well-paraphrased citations. It will miss some real problems.
   This tradeoff was made on purpose - don't "fix" it by raising the threshold
   without re-running the full test suite, since
   `test_paraphrased_but_related_citation_is_not_flagged_as_weak` exists
   specifically to catch that regression.
7. **The external researcher over-produces.** Its task asks for 8-15 claims;
   live runs returned 31, then 20 after the instruction was tightened to "stop
   once you have that many". All were well-sourced, so this is a
   prompt-adherence gap rather than a correctness bug, but tighten it further if
   the Analysis Crew struggles with the volume.
8. **Internal document filenames are part of the deliverable.** They're cited
   verbatim in the report's Sources appendix, so they follow a consistent
   `internal_*` convention and are spelled correctly. Three tests assert
   specific filenames; renaming a doc means updating
   `tests/test_internal_kb_tool.py`.

## How to resume a session on this project

1. Read this file, then the documents in `knowledge/` (`style_guide.md` first -
   it defines the report the whole pipeline is building toward).
2. Run the whole flow end-to-end next, then write `main.py` and the README.
   Every stage can be developed against saved output in `sample_runs/` instead
   of paying for upstream runs - `ResearchFindings`, `AnalysisResult`, and
   `FinalReport` all load with `.model_validate_json(path.read_text())`.
3. Run `load_crew()` on any new JSONC config before a live run - it catches
   config errors for free.
4. Run `pytest` after every change that touches existing tested code. The
   project's convention throughout has been "don't claim something works, run
   it and show the output."
