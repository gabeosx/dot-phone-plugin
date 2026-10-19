import copy
import json

from dots_phone.source_search import source_activity


def test_legacy_and_failed_responses_remain_distinct_from_read_evidence():
    call = {
        "source_requests": [
            {"operation": "search", "arguments": {"query": "private-query"}, "searches": []},
            {"operation": "search", "source_error": True},
            {
                "operation": "search",
                "searches": [
                    {
                        "provider": "gmail",
                        "status": "error",
                        "more_available": False,
                        "query": "private-query",
                    },
                    {
                        "provider": "yahoo",
                        "status": "unavailable",
                        "more_available": False,
                        "query": "private-query",
                        "mailbox": "INBOX",
                    },
                ],
            },
            {"operation": "read", "source_error": True},
        ],
        "retrieved_sources": {"private-id": {"text": "private-body"}},
    }
    original = copy.deepcopy(call)
    activity = source_activity(call)
    assert activity == {
        "search_requests": 3,
        "lookup_requests": 0,
        "successful_lookup_requests": 0,
        "read_requests": 1,
        "successful_read_requests": 0,
        "worker_errors": 2,
        "search_requests_without_provider_trace": 2,
        "provider_searches": [
            {"provider": "gmail", "status": "error", "more_available": False},
            {"provider": "yahoo", "status": "unavailable", "more_available": False},
        ],
    }
    assert "private" not in json.dumps(activity)
    assert call == original


def test_result_without_source_responses_does_not_claim_an_investigation(service, brief):
    prepared = service.prepare("owner", brief)
    call = service.call("owner", prepared["call_id"])
    service.terminal(call, "failed", "decision_timeout")
    before = service.call("owner", call["call_id"])
    result = service.result("owner", call["call_id"])
    assert result["source_activity"] == {
        "search_requests": 0,
        "lookup_requests": 0,
        "successful_lookup_requests": 0,
        "read_requests": 0,
        "successful_read_requests": 0,
        "worker_errors": 0,
        "search_requests_without_provider_trace": 0,
        "provider_searches": [],
    }
    assert result["outcome"] == "unresolved"
    assert service.call("owner", call["call_id"]) == before


def test_local_delivered_responses_are_counted_without_inventing_provider_coverage():
    call = {
        "source_requests": [{"request_id": "shared", "operation": "read"}],
        "usage": {
            "local_context_worker": {
                "requests": [
                    {"request_id": "shared", "operation": "read", "result_delivered_ms": 1},
                    {"request_id": "lookup", "operation": "search", "result_delivered_ms": 2},
                    {"request_id": "pending", "operation": "read"},
                    {"request_id": "report", "operation": "complete", "result_delivered_ms": 3},
                ]
            }
        },
    }
    summary = source_activity(call)
    assert summary["search_requests"] == 1
    assert summary["read_requests"] == summary["successful_read_requests"] == 1
    assert summary["search_requests_without_provider_trace"] == 1
    assert summary["provider_searches"] == []
