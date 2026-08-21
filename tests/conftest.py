"""
Shared test fixtures.

The usage ledger is process-wide (see costs.UsageLedger's docstring for why),
which makes it the one piece of state that can leak between tests. It did:
test_research_crew_merge.py runs a real crew with stubbed LLMs, which records
a stage, and test_main.py then saw a cost.json it did not expect and failed -
but only when run after it, never alone.

Resetting before every test makes the suite order-independent, and is the
price of the ledger being a singleton.
"""

import pytest  # pyrefly: ignore

from crewai_exec_deep_research_agent.costs import LEDGER


@pytest.fixture(autouse=True)
def _reset_usage_ledger():
    LEDGER.reset()
    yield
    LEDGER.reset()
