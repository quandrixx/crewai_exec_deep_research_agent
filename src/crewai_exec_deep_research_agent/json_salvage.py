"""
Recovering a JSON object from an agent's free-text answer.

**Why this exists.** Agents that carry tools do not get native structured
output from CrewAI - `crew_agent_executor.py` passes `response_model` only when
an agent has no tools, so the research agents' output is parsed out of free
text by a Converter. That is a weaker guarantee, and it fails in a specific,
recoverable way: the model produces perfectly good JSON and then wraps it in a
markdown fence, or writes a sentence before it.

**Why recovering it matters more than it looks.** A guardrail rejection is not
cheap. CrewAI answers one by calling `agent.execute_task(...)` again - a fresh
ReAct loop that re-runs every tool call. A measured run bounced twice and paid
for two extra rounds of web searches to fix output that was already correct
apart from its wrapper. Worse, `guardrail_max_retries` is a hard limit:
exhausting it raises and kills the run. Salvaging locally turns an expensive,
failure-prone retry into a deterministic string operation.

This only unwraps - it never repairs. Truncated or genuinely malformed JSON
still fails, and should: inventing structure to make a parse succeed would hide
exactly the truncation bug that once cost 15 real claims.
"""

import json
from typing import Any


def salvage_json_object(raw: str | None, required_key: str) -> tuple[str, Any] | None:
    """Find a JSON object containing `required_key` inside `raw`.

    Returns `(json_text, parsed_object)` on success, or None when nothing
    usable is there. The raw text is returned alongside the parsed object
    because CrewAI's guardrail contract re-exports a returned string through
    the task's `output_pydantic` - handing back the cleaned text is what
    repairs the TaskOutput rather than merely reporting on it.

    `required_key` guards against grabbing the wrong object: an answer often
    contains several JSON-ish fragments (a tool call it echoed, an example it
    quoted), and only one of them is the payload.
    """
    if not raw:
        return None

    for candidate in _candidate_objects(raw):
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict) and required_key in parsed:
            return candidate, parsed
    return None


def _candidate_objects(raw: str) -> list[str]:
    """Every balanced {...} span in the text, outermost first.

    Scans with a brace counter rather than a regex because the payloads here
    nest (a ClaimList holds a list of claim objects) and regexes cannot match
    balanced delimiters. String-aware so a brace inside a claim's text - or an
    escaped quote - doesn't throw the count off.
    """
    spans: list[str] = []
    depth = 0
    start = -1
    in_string = False
    escaped = False

    for index, char in enumerate(raw):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    spans.append(raw[start : index + 1])

    # Longest first: the outermost object is the whole payload, and a nested
    # one could coincidentally carry the same key.
    return sorted(spans, key=len, reverse=True)
