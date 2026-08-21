"""
Locating the repository's knowledge/ directory.

The mock corpus and house-style references live at the repo root, not inside
the installed package, so a fixed relative path breaks depending on where the
process was started from. Walking up from this file covers both the editable
install used in development and a plain `python -m` run from any directory.

Shared because two callers need it for different reasons: internal_kb_tool
reads knowledge/internal_docs/ as retrievable evidence, and the Report Crew
reads knowledge/style_guide.md and knowledge/prior_exec_report_sample.md as
writing references. Only the former is ever citable as a source.
"""

import os
from pathlib import Path


_ENV_OVERRIDE = "KNOWLEDGE_DIR"


def find_knowledge_dir() -> Path:
    """Return the knowledge/ directory.

    KNOWLEDGE_DIR overrides the search entirely, which is also the seam a real
    deployment would use to point at a mounted document store.
    """
    override = os.getenv(_ENV_OVERRIDE)
    if override:
        return Path(override)

    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "knowledge"
        if candidate.is_dir():
            return candidate

    # Nothing found - return the conventional location so error messages name
    # a concrete path rather than nothing at all.
    return here.parents[2] / "knowledge"


def read_knowledge_file(name: str) -> str:
    """Read a file from knowledge/, failing loudly if it isn't there.

    Loud on purpose: the Report Crew's whole job is matching house style, and
    silently proceeding with an empty style guide would produce a plausible
    report that quietly ignores every rule it was supposed to follow.
    """
    path = find_knowledge_dir() / name
    if not path.is_file():
        raise FileNotFoundError(
            f"Required knowledge file not found: {path}. The Report Crew needs "
            f"this to match house style; refusing to generate a report without "
            f"it. Set {_ENV_OVERRIDE} if the knowledge directory has moved."
        )
    return path.read_text()
