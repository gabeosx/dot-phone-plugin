"""Structural search evidence, without interpreting identities or search meaning."""


def source_activity(call):
    """Summarize recorded worker responses without exposing mailbox content.

    A successful search only describes execution of that query. It establishes
    neither absence from a mailbox nor that a returned message was read.
    """
    history = list(call.get("source_requests", []))
    recorded = {item["request_id"] for item in history if "request_id" in item}
    # Local transports record a request only after validating and delivering its
    # result. Older records have no provider trace; preserve that uncertainty.
    history.extend(
        item
        for item in call.get("usage", {}).get("local_context_worker", {}).get("requests", [])
        if item.get("operation") in {"search", "read"}
        and item.get("result_delivered_ms") is not None
        and item.get("request_id") not in recorded
    )
    searches = [item for item in history if item["operation"] == "search"]
    reads = [item for item in history if item["operation"] == "read"]
    lookups = [item for item in history if item["operation"] == "lookup"]
    return {
        "search_requests": len(searches),
        "lookup_requests": len(lookups),
        "successful_lookup_requests": sum(not item.get("source_error", False) for item in lookups),
        "read_requests": len(reads),
        "successful_read_requests": sum(not item.get("source_error", False) for item in reads),
        "worker_errors": sum(bool(item.get("source_error")) for item in history),
        "search_requests_without_provider_trace": sum(
            not item.get("searches") for item in searches
        ),
        "provider_searches": [
            {key: attempt[key] for key in ("provider", "status", "more_available")}
            for item in searches
            for attempt in item.get("searches", [])
        ],
    }


def validate_search_result(output, arguments):
    attempts = output.get("searches")
    # The original native answer envelope remains compatible, with unknown
    # coverage. New provider-selecting requests require an actual search trace.
    if attempts is None:
        if "provider" in arguments:
            raise ValueError("search coverage missing")
        return
    if not isinstance(attempts, list) or not 1 <= len(attempts) <= 2:
        raise ValueError("invalid search coverage")
    providers = set()
    for attempt in attempts:
        if (
            not isinstance(attempt, dict)
            or not {"provider", "query", "status", "more_available"} <= set(attempt)
            or not set(attempt) <= {"provider", "query", "status", "more_available", "mailbox"}
            or attempt["provider"] not in {"gmail", "yahoo"}
            or attempt["provider"] in providers
            or attempt["status"] not in {"ok", "error", "unavailable"}
            or type(attempt["more_available"]) is not bool
            or not isinstance(attempt["query"], str)
            or not 1 <= len(attempt["query"]) <= 240
            or ("provider" in arguments and attempt["query"] != arguments["query"])
            or ("mailbox" in attempt and attempt["mailbox"] != "INBOX")
        ):
            raise ValueError("invalid search coverage")
        providers.add(attempt["provider"])
    selected = arguments.get("provider", "all")
    if "provider" in arguments and providers != (
        {"gmail", "yahoo"} if selected == "all" else {selected}
    ):
        raise ValueError("search coverage differs from request")
    successful = {a["provider"] for a in attempts if a["status"] == "ok"}
    for message in output["messages"]:
        if not isinstance(message, dict) or message.get("provider") not in successful:
            raise ValueError("message has no successful provider search")
