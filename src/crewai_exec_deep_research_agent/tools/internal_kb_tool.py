"""
internal_kb_tool.py

CrewAI tool wrapping the mock internal knowledge base (knowledge/internal_docs/).
Retrieval is deliberately simple - paragraph-level keyword overlap, no
embeddings or vector store - for the same reason citation_check_tool is
deterministic: this is a stand-in for a real client integration (Confluence,
SharePoint, an internal wiki, a vector DB over deal memos), and the take-home
prompt explicitly says to mock internal sources as static files. In a real
deployment, swap this tool's _load_documents()/_search() internals for a real
retrieval backend behind the same CrewAI tool interface - the Research Crew's
agent config doesn't need to change at all.

Chunking strategy: split each markdown file on blank lines into paragraph-ish
chunks, since these mock docs are already written in short paragraphs. A real
backend would chunk more carefully (headers, token limits, overlap windows).
"""

import os
import re
from pathlib import Path
from typing import Type

from pydantic import BaseModel, Field
from crewai.tools import BaseTool


_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with",
    "is", "are", "was", "were", "this", "that", "these", "those", "as",
    "by", "at", "it", "its", "be", "has", "have", "had", "will", "we",
    "our", "than", "into", "from", "not", "no", "which", "their", "them",
}

def _find_internal_docs_dir() -> Path:
    """Locate knowledge/internal_docs/ by walking up from this file.

    The docs live at the repository root, not inside the installed package, so
    a fixed relative path breaks depending on where the process starts. Walking
    up covers both the editable install used in development and a plain
    `python -m` run from any working directory. INTERNAL_DOCS_DIR overrides
    this entirely, which is also the seam a real deployment would use to point
    at a mounted document store instead.
    """
    override = os.getenv("INTERNAL_DOCS_DIR")
    if override:
        return Path(override)
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "knowledge" / "internal_docs"
        if candidate.is_dir():
            return candidate
    # Nothing found - return the conventional location so the error message
    # downstream names a concrete path rather than nothing at all.
    return here.parents[3] / "knowledge" / "internal_docs"


_INTERNAL_DOCS_DIR = _find_internal_docs_dir()
_TOP_K = 4


def _significant_words(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {w for w in words if w not in _STOPWORDS and len(w) > 2}


def _load_chunks() -> list[dict]:
    """Returns a list of {source, heading, text} chunks across all internal docs."""
    chunks = []
    if not _INTERNAL_DOCS_DIR.is_dir():
        return chunks
    for path in sorted(_INTERNAL_DOCS_DIR.glob("*.md")):
        text = path.read_text()
        current_heading = path.stem
        # split on blank lines; track the most recent markdown heading seen
        for block in re.split(r"\n\s*\n", text):
            block = block.strip()
            if not block:
                continue
            heading_match = re.match(r"^#{1,3}\s+(.*)", block)
            if heading_match:
                current_heading = heading_match.group(1).strip()
                continue  # headings themselves aren't retrievable content
            chunks.append({
                "source": path.name,
                "heading": current_heading,
                "text": block,
            })
    return chunks


# Loaded once at import time - these are static files, no need to re-read
# the filesystem on every tool call.
_CHUNKS = _load_chunks()


def known_document_names() -> set[str]:
    """Every filename the tool can legitimately cite.

    Public because the Research Crew's guardrail checks internal citations
    against this set - a claim citing a document that doesn't exist is a
    fabrication, and that's cheap to catch deterministically rather than
    hoping the fact-check gate notices later.
    """
    return {chunk["source"] for chunk in _CHUNKS}


def _search(query: str, top_k: int = _TOP_K) -> list[dict]:
    query_words = _significant_words(query)
    scored = []
    for chunk in _CHUNKS:
        overlap = len(query_words & _significant_words(chunk["text"]))
        if overlap > 0:
            scored.append((overlap, chunk))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [chunk for _, chunk in scored[:top_k]]


class InternalKBSearchInput(BaseModel):
    query: str = Field(
        description="Keywords or a natural-language question describing what "
                     "internal information to look for, e.g. 'prior diligence on "
                     "enhanced geothermal' or 'LP sentiment on advanced nuclear'."
    )


class InternalKBLookupTool(BaseTool):
    name: str = "internal_kb_lookup"
    description: str = (
        "Searches Northbridge Ventures' internal knowledge base - past investment "
        "theses, diligence memos, scouting notes, portfolio check-ins, and sector "
        "scans. Use this to find what the firm already knows or has previously "
        "concluded about a topic, as distinct from public/external information. "
        "Always cite the exact 'source' filename returned when making a claim."
    )
    args_schema: Type[BaseModel] = InternalKBSearchInput

    def _run(self, query: str) -> str:
        # An empty index and an empty result set mean very different things to
        # the agent: one is a broken deployment, the other is a real finding.
        # Collapsing them would let a misconfigured knowledge base masquerade
        # as "Northbridge has no view on this."
        if not _CHUNKS:
            return (
                f"The internal knowledge base could not be loaded (no documents "
                f"found at {_INTERNAL_DOCS_DIR}). This is a configuration "
                f"problem, NOT a finding - do not report that Northbridge has "
                f"no internal work on this topic, and do not fabricate internal "
                f"views. Report that the internal knowledge base was unavailable."
            )
        results = _search(query)
        if not results:
            return (
                "No internal documents matched this query. This likely means "
                "Northbridge has no prior internal work on this specific topic - "
                "state that explicitly rather than inferring internal views from "
                "adjacent or unrelated documents."
            )
        formatted = []
        for chunk in results:
            formatted.append(
                f"[source: {chunk['source']} | section: {chunk['heading']}]\n"
                f"{chunk['text']}"
            )
        return "\n\n---\n\n".join(formatted)