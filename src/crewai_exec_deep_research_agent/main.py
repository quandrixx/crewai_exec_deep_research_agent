#!/usr/bin/env python
"""
Entry point for the Deep Research Agent.

    crewai run                         # default demo topic
    uv run kickoff "enhanced geothermal systems"
    uv run kickoff --list-topics
    uv run plot                        # render the flow diagram

`crewai run` resolves a flow project to the `kickoff` script declared in
pyproject.toml and loads .env first; the explicit load_dotenv() below is for
everything else (`uv run kickoff`, `python -m`, a test harness), so the two
paths behave identically.

Two decisions here are deliberate and worth keeping.

**Every stage's output is written to disk, including when the run escalates.**
The Flow withholds the report if the fact-check gate fails twice, and the whole
point of that path is to give a human analyst a head start rather than making
them start the topic over. Printing the failed citations and discarding the
research and analysis that produced them would defeat it.

**The exit code distinguishes "no report" from "crashed".** An escalation is
the safety machinery working correctly, not a bug - but it is also not a
delivered report, and a caller scripting this needs to tell the difference.
"""

import json
import os
import sys
from pathlib import Path

from crewai_exec_deep_research_agent.flows.deep_research_flow import DeepResearchFlow


DEFAULT_TOPIC = "small modular reactors"
OUTPUT_DIR = Path("output")
TOPICS_FILE = Path("sample_runs/topics.json")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_ESCALATED = 2


def _load_env() -> None:
    """Load .env if present. Harmless when the CLI already did it."""
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - dotenv ships with crewai
        return
    env_file = Path(".env")
    if env_file.is_file():
        load_dotenv(env_file)


def _check_api_keys() -> None:
    """Fail fast on a missing model key, warn on a missing search key.

    The distinction matters. Without ANTHROPIC_API_KEY nothing can run at all,
    so stopping immediately beats burning a minute to fail mid-crew. Without
    SERPER_API_KEY the run still produces a briefing from internal sources
    alone - web_search_tool returns an explicit "do not fabricate" message
    rather than failing - but the result is materially weaker, and someone
    reading the output later deserves to know that was the case.
    """
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise SystemExit(
            "ANTHROPIC_API_KEY is not set. Add it to .env or the environment - "
            "every crew in this pipeline needs it."
        )
    if not os.getenv("SERPER_API_KEY"):
        print(
            "WARNING: SERPER_API_KEY is not set, so external web research will "
            "return nothing. The briefing will be built from internal sources "
            "only and will be much weaker. Set it in .env for a real run.\n",
            file=sys.stderr,
        )


def _slug(topic: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in topic.lower()).strip("_")


def _suggested_topics() -> list[str]:
    if not TOPICS_FILE.is_file():
        return [DEFAULT_TOPIC]
    try:
        data = json.loads(TOPICS_FILE.read_text())
        return [t["topic"] for t in data.get("topics", [])] or [DEFAULT_TOPIC]
    except (json.JSONDecodeError, KeyError, TypeError):
        return [DEFAULT_TOPIC]


def _save(path: Path, model) -> Path:
    path.write_text(json.dumps(model.model_dump(mode="json"), indent=2))
    return path


def _save_artifacts(state, out_dir: Path) -> list[Path]:
    """Persist whatever the run produced, however far it got."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    if state.research is not None:
        written.append(_save(out_dir / "research.json", state.research))
    if state.analysis is not None:
        written.append(_save(out_dir / "analysis.json", state.analysis))
    if state.fact_check is not None:
        written.append(_save(out_dir / "fact_check.json", state.fact_check))
    if state.final_report is not None:
        from crewai_exec_deep_research_agent.crews.report_crew.report_crew import (
            render_markdown,
        )

        written.append(_save(out_dir / "report.json", state.final_report))
        report_md = out_dir / "report.md"
        report_md.write_text(render_markdown(state.final_report))
        written.append(report_md)

    return written


def _print_summary(state, written: list[Path]) -> None:
    print("\n" + "=" * 70)
    print(f"TOPIC: {state.topic}")

    if state.research is not None:
        print(
            f"  research   : {len(state.research.external_claims)} external + "
            f"{len(state.research.internal_claims)} internal claims"
        )
    if state.analysis is not None:
        a = state.analysis
        print(
            f"  analysis   : {len(a.market_shifts)} shifts, "
            f"{len(a.incumbents) + len(a.new_entrants)} companies, "
            f"{len(a.funding_events)} funding events, "
            f"{len(a.tensions_or_conflicts)} tensions, "
            f"{len(a.recommendations)} recommendations"
        )
    if state.fact_check is not None:
        f = state.fact_check
        print(
            f"  fact-check : {'PASSED' if f.passed else 'FAILED'} "
            f"({f.verified_count} citations verified, {len(f.issues)} issues, "
            f"{state.revision_count} revision round(s))"
        )

    if written:
        print("\n  written:")
        for path in written:
            print(f"    {path}")

    if state.final_report is not None:
        r = state.final_report
        print(f"\n  {r.title}")
        print(
            f"  {len(r.body_markdown.split())} words, "
            f"{len(r.sources_appendix)} sources, "
            f"fact-check status: {r.fact_check_status}"
        )
    else:
        # The Flow already printed the specific failed citations.
        print(
            "\n  NO REPORT PRODUCED - the fact-check gate rejected the analysis "
            "twice.\n  The research and analysis above are saved so a human "
            "analyst can pick up\n  from the failed citations rather than "
            "restarting the topic."
        )
    print("=" * 70)


def _run(topic: str) -> int:
    _check_api_keys()

    out_dir = OUTPUT_DIR / _slug(topic)
    flow = DeepResearchFlow()

    try:
        flow.kickoff(inputs={"topic": topic})
    except Exception as exc:
        # Save whatever completed before re-raising the news. A crash in the
        # report stage should not throw away several minutes of research and
        # analysis - the state carries every finished stage, and losing it is
        # the same mistake as discarding work on the escalation path.
        written = _save_artifacts(flow.state, out_dir)
        print(f"\nRun failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        if written:
            print(
                "Completed stages were still saved:\n  "
                + "\n  ".join(str(p) for p in written),
                file=sys.stderr,
            )
        else:
            print("No stage completed, so nothing was saved.", file=sys.stderr)
        return EXIT_ERROR

    written = _save_artifacts(flow.state, out_dir)
    _print_summary(flow.state, written)

    return EXIT_OK if flow.state.final_report is not None else EXIT_ESCALATED


def kickoff() -> None:
    """Run the full pipeline. Entry point for `crewai run` and `uv run kickoff`."""
    _load_env()

    args = [a for a in sys.argv[1:] if a.strip()]

    if args and args[0] in ("--list-topics", "-l"):
        print("Suggested topics:")
        for topic in _suggested_topics():
            print(f"  {topic}")
        raise SystemExit(EXIT_OK)

    # `crewai run` invokes this with no arguments, so a default is required
    # for the zero-config path to work at all.
    topic = " ".join(args) if args else os.getenv("RESEARCH_TOPIC", DEFAULT_TOPIC)
    raise SystemExit(_run(topic))


def plot() -> None:
    """Render the Flow's structure to an HTML diagram.

    Two adjustments to CrewAI's plot(): show=False, because this runs headless
    as often as not and it otherwise tries to open a browser; and the output is
    copied into the project, because CrewAI writes to a temp directory that
    gets cleaned up regardless of the filename passed in.

    The whole directory is copied, not just the .html. plot() emits three
    files - the page, a ~110KB script holding the actual graph data, and a
    stylesheet - and the page loads its siblings by relative path, so copying
    the .html alone yields a blank diagram that looks like it worked.

    The warnings CrewAI emits about routers not being statically inferable are
    expected. Both routers pick a label from a conditional, which its static
    pass cannot follow - runtime behavior is unaffected, and
    tests/test_flow_wiring.py covers the routing directly.
    """
    import shutil

    _load_env()
    generated = Path(DeepResearchFlow().plot(filename="crewai_flow.html", show=False))

    destination = OUTPUT_DIR / "flow"
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copytree(generated.parent, destination, dirs_exist_ok=True)
    print(f"Flow diagram written to {destination / generated.name}")


def run_with_trigger() -> None:
    """Run from a JSON trigger payload, for CrewAI AMP deployments.

    Kept because pyproject.toml declares it as a script and AMP calls it. The
    payload's `topic` is used when present.
    """
    _load_env()

    if len(sys.argv) < 2:
        raise SystemExit(
            "No trigger payload provided. Pass a JSON object as the first "
            'argument, e.g. \'{"topic": "small modular reactors"}\''
        )

    try:
        payload = json.loads(sys.argv[1])
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Trigger payload is not valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise SystemExit("Trigger payload must be a JSON object.")

    raise SystemExit(_run(payload.get("topic", DEFAULT_TOPIC)))


if __name__ == "__main__":
    kickoff()
