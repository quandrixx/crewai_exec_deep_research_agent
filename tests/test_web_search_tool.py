"""
test_web_search_tool.py

All requests.post calls are mocked here. This tests the tool's own
behavior (formatting, error handling, missing-key handling) in isolation
from Serper's actual API.
"""

from unittest.mock import patch, Mock
import requests

from crewai_exec_deep_research_agent.tools.web_search_tool import WebSearchTool


def make_mock_response(json_data: dict, status_code: int = 200) -> Mock:
    mock_resp = Mock()
    mock_resp.status_code = status_code
    mock_resp.json.return_value = json_data
    mock_resp.raise_for_status = Mock()
    if status_code >= 400:
        mock_resp.raise_for_status.side_effect = requests.exceptions.HTTPError(
            f"{status_code} error"
        )
    return mock_resp


# ---------------------------------------------------------------------------
# Missing API key
# ---------------------------------------------------------------------------

def test_missing_api_key_returns_explicit_message_not_exception():
    tool = WebSearchTool()
    with patch.dict("os.environ", {}, clear=True):
        result = tool._run("small modular reactor licensing")
    assert "SERPER_API_KEY is not set" in result
    assert "Do not fabricate" in result


# ---------------------------------------------------------------------------
# Successful responses
# ---------------------------------------------------------------------------

@patch("crewai_exec_deep_research_agent.tools.web_search_tool.requests.post")
def test_successful_response_includes_source_urls(mock_post):
    mock_post.return_value = make_mock_response({
        "organic": [
            {
                "title": "Company A raises $42M Series A",
                "link": "https://example-news.test/company-a-series-a",
                "snippet": "Company A announced a $42M Series A round today.",
            },
            {
                "title": "Molten salt reactor developer secures funding",
                "link": "https://example-news.test/msr-funding",
                "snippet": "A molten salt reactor startup closed new funding.",
            },
        ]
    })
    with patch.dict("os.environ", {"SERPER_API_KEY": "fake-key-for-test"}):
        result = WebSearchTool()._run("advanced nuclear funding")

    assert "[source: https://example-news.test/company-a-series-a]" in result
    assert "[source: https://example-news.test/msr-funding]" in result
    assert "Company A raises $42M Series A" in result


@patch("crewai_exec_deep_research_agent.tools.web_search_tool.requests.post")
def test_results_are_truncated_to_top_k(mock_post):
    organic_results = [
        {"title": f"Result {i}", "link": f"https://example.test/{i}", "snippet": "..."}
        for i in range(10)
    ]
    mock_post.return_value = make_mock_response({"organic": organic_results})
    with patch.dict("os.environ", {"SERPER_API_KEY": "fake-key-for-test"}):
        result = WebSearchTool()._run("broad query")

    assert result.count("[source:") == 5  # _TOP_K


@patch("crewai_exec_deep_research_agent.tools.web_search_tool.requests.post")
def test_empty_organic_results_returns_explicit_no_results_message(mock_post):
    mock_post.return_value = make_mock_response({"organic": []})
    with patch.dict("os.environ", {"SERPER_API_KEY": "fake-key-for-test"}):
        result = WebSearchTool()._run("an extremely obscure query")

    assert "No external web results found" in result


# ---------------------------------------------------------------------------
# Failure handling - each should degrade gracefully, never raise
# ---------------------------------------------------------------------------

@patch("crewai_exec_deep_research_agent.tools.web_search_tool.requests.post")
def test_http_error_is_caught_and_reported_not_raised(mock_post):
    mock_post.return_value = make_mock_response({}, status_code=500)
    with patch.dict("os.environ", {"SERPER_API_KEY": "fake-key-for-test"}):
        result = WebSearchTool()._run("some query")

    assert "Web search request failed" in result
    assert "Do not fabricate" in result


@patch("crewai_exec_deep_research_agent.tools.web_search_tool.requests.post")
def test_timeout_is_caught_and_reported_with_specific_message(mock_post):
    mock_post.side_effect = requests.exceptions.Timeout()
    with patch.dict("os.environ", {"SERPER_API_KEY": "fake-key-for-test"}):
        result = WebSearchTool()._run("some query")

    assert "timed out" in result
    assert "Do not fabricate" in result


@patch("crewai_exec_deep_research_agent.tools.web_search_tool.requests.post")
def test_connection_error_is_caught_and_reported(mock_post):
    mock_post.side_effect = requests.exceptions.ConnectionError("DNS failure")
    with patch.dict("os.environ", {"SERPER_API_KEY": "fake-key-for-test"}):
        result = WebSearchTool()._run("some query")

    assert "Web search request failed" in result


@patch("crewai_exec_deep_research_agent.tools.web_search_tool.requests.post")
def test_unparseable_json_response_is_handled(mock_post):
    mock_resp = Mock()
    mock_resp.raise_for_status = Mock()
    mock_resp.json.side_effect = ValueError("not valid json")
    mock_post.return_value = mock_resp
    with patch.dict("os.environ", {"SERPER_API_KEY": "fake-key-for-test"}):
        result = WebSearchTool()._run("some query")

    assert "unreadable response" in result


# ---------------------------------------------------------------------------
# Confirm the tool calls the network exactly once per query, with the
# expected endpoint - i.e. the mock genuinely intercepted the call rather
# than the tool silently doing something else.
# ---------------------------------------------------------------------------

@patch("crewai_exec_deep_research_agent.tools.web_search_tool.requests.post")
def test_search_hits_expected_endpoint_exactly_once(mock_post):
    mock_post.return_value = make_mock_response({"organic": []})
    with patch.dict("os.environ", {"SERPER_API_KEY": "fake-key-for-test"}):
        WebSearchTool()._run("test query")

    mock_post.assert_called_once()
    called_args, called_kwargs = mock_post.call_args
    called_url = called_args[0] if called_args else called_kwargs.get("url")
    assert called_url == "https://google.serper.dev/search"
    assert called_kwargs["json"]["q"] == "test query"