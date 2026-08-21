"""
web_search_tool.py

CrewAI tool for external/public web research. Wraps Serper.dev
(https://serper.dev), a lightweight Google Search API wrapper. 
Swap _call_serper()'s internals for Tavily, Bing, or another provider behind 
this same tool interface if preferred; nothing else in the crew config needs to change, since the Research Crew only 
ever sees this tool's name/description/schema.

Output is deliberately formatted to mirror internal_kb_tool's
"[source: ...]" convention - same citation shape for both internal and
external evidence, so the Research Crew's agents (and later, the
deterministic citation-check gate) treat both kinds of source uniformly
rather than the report ending up with two different citation styles.

Failure modes are handled explicitly rather than silently, and each
failure message includes an instruction not to fabricate - the same
principle as internal_kb_tool's "no match" message: an agent should never
have to guess whether an empty/failed tool call means "nothing exists"
versus "the tool broke."
"""

import os
from typing import Type

import requests
from pydantic import BaseModel, Field
from crewai.tools import BaseTool

_SERPER_URL = "https://google.serper.dev/search"
_TOP_K = 5
_TIMEOUT_SECONDS = 10


class WebSearchInput(BaseModel):
    query: str = Field(
        description="A specific, targeted search query - e.g. 'small modular "
                     "reactor NRC licensing 2026' rather than a broad topic "
                     "name alone. Narrower queries return more usable results."
    )


class WebSearchTool(BaseTool):
    name: str = "web_search"
    description: str = (
        "Searches the public internet for current, external information - "
        "news, company announcements, funding data, industry analysis. Use "
        "this for anything Northbridge Ventures would need to look up "
        "externally, as distinct from the firm's own internal knowledge base. "
        "Always cite the exact 'source' URL returned when making a claim, "
        "and paraphrase findings in your own words rather than quoting "
        "snippets verbatim."
    )
    args_schema: Type[BaseModel] = WebSearchInput

    def _run(self, query: str) -> str:
        api_key = os.getenv("SERPER_API_KEY")
        if not api_key:
            return (
                "SERPER_API_KEY is not set, so external web search is "
                "unavailable. Do not fabricate external findings for this "
                "query - report explicitly that external research could not "
                "be performed."
            )

        try:
            response = requests.post(
                _SERPER_URL,
                headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
                json={"q": query, "num": _TOP_K},
                timeout=_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
        except requests.exceptions.Timeout:
            return (
                f"Web search timed out after {_TIMEOUT_SECONDS}s for query "
                f"'{query}'. Do not fabricate external findings - report that "
                "this specific search failed and could be retried."
            )
        except requests.exceptions.RequestException as e:
            return (
                f"Web search request failed for query '{query}' ({e}). Do "
                "not fabricate external findings - report that external "
                "research failed for this query."
            )

        try:
            data = response.json()
        except ValueError:
            return (
                f"Web search returned an unreadable response for query "
                f"'{query}'. Do not fabricate external findings."
            )

        organic = data.get("organic", [])
        if not organic:
            return f"No external web results found for query '{query}'."

        formatted = []
        for result in organic[:_TOP_K]:
            title = result.get("title", "Untitled")
            link = result.get("link", "")
            snippet = result.get("snippet", "")
            formatted.append(f"[source: {link}]\n{title}\n{snippet}")

        return "\n\n---\n\n".join(formatted)