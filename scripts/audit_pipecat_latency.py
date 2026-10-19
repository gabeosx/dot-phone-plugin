"""Attribute carrier answer delay without treating context acknowledgements as speech."""

import argparse
import json
from pathlib import Path


def measure(directory):
    carrier = json.loads((directory / "carrier-result.json").read_text())
    response = json.loads((directory / "responsiveness.json").read_text())
    caller = carrier["owned-bot-status.json"]["calls"][carrier["call_sid"]]
    assistant = carrier["assistant"][0]
    timing = json.loads((directory / (assistant["call_sid"] + "-timing.json")).read_text())
    last = next(t for t in caller["turns"] if t["name"] == "interrupt")[
        "last_voice_sent_at_seconds"
    ]
    interrupt_start = next(t for t in caller["turns"] if t["name"] == "interrupt")["at_seconds"]
    hangup = next(t for t in caller["turns"] if t["name"] == "hangup")["at_seconds"]
    shift = timing["clock_origin_monotonic"] - caller["clock_origin_monotonic"]
    events = [
        {**e, "from_last_voice_ms": round((shift + e["at_ms"] / 1000 - last) * 1000)}
        for e in timing["timeline"]
        if interrupt_start <= shift + e["at_ms"] / 1000 < hangup
    ]

    def first(kind, **fields):
        return next(
            (
                e
                for e in events
                if e["kind"] == kind and all(e.get(k) == v for k, v in fields.items())
            ),
            None,
        )

    started = first("backend_event", event_type="response.created")
    realtime_started = first("realtime_response", event_type="response.created")
    lookup = first("tool_started", name="lookup_hours")
    lookup_complete = first("lookup_completed")
    prefetch_started = first("prefetch_started")
    prefetch_ready = first("prefetch_ready")
    output = first("backend_command_sent", event_type="response.item.create")
    followup = first("backend_command_sent", event_type="response.create")
    text = first("backend_event", event_type="response.output_text.delta")
    forwarded = first("verified_result_forwarded")
    ack = (
        first("context_acknowledged", client_event_id=forwarded["event_id"]) if forwarded else None
    )
    completed = [
        e
        for e in events
        if e["kind"] == "backend_event" and e["event_type"] == "response.completed"
    ]
    tiers = [e.get("service_tier") for e in completed]
    config = assistant["backend_configuration"]

    def at(event):
        return event["from_last_voice_ms"] if event else None

    result = {
        "configuration": config,
        "backend_started_ms": at(started),
        "realtime_response_started_ms": at(realtime_started),
        "lookup_started_ms": at(lookup),
        "tool_output_command_sent_ms": at(output),
        "continuation_command_sent_ms": at(followup),
        "continuation_first_text_ms": at(text),
        "early_verified_result_sent_ms": at(forwarded),
        "early_verified_result_ack_ms": at(ack),
        "prefetch_started_ms": at(prefetch_started),
        "prefetch_ready_ms": at(prefetch_ready),
        "after_verified_lookup_ms": response["closing_time_after_last_caller_voice_ms"]
        - at(lookup_complete)
        if lookup_complete and response["closing_time_after_last_caller_voice_ms"] is not None
        else None,
        "backend_completed_ms": [at(e) for e in completed],
        "backend_resolved_service_tiers": tiers,
        "heard_closing_time_ms": response["closing_time_after_last_caller_voice_ms"],
        "heard_yield_ms": response["yield_first_300ms_quiet_ms"],
        "remaining_after_tool_output_ms": response["closing_time_after_last_caller_voice_ms"]
        - at(output)
        if output and response["closing_time_after_last_caller_voice_ms"] is not None
        else None,
        "assertions": {
            "functional_and_audio_passed": response["responsiveness_passed"],
            "realtime_tool_outputs_confirmed": config.get("engine") != "realtime"
            or assistant.get("model_usage", {}).get("tool_outputs_transmitted") is True,
            "early_result_acknowledged_if_enabled": not config.get("early_verified_result")
            or ack is not None,
            "prefetch_exercised_if_enabled": not config.get("prefetch")
            or prefetch_ready is not None,
            "answer_after_verified_lookup": lookup_complete is not None
            and response["closing_time_after_last_caller_voice_ms"] is not None
            and response["closing_time_after_last_caller_voice_ms"] >= at(lookup_complete),
            "priority_reported_if_requested": config.get("service_tier_requested") != "priority"
            or bool(tiers)
            and all(t == "priority" for t in tiers),
        },
        "limits": "Local monotonic clocks attribute app/provider stages. Append acknowledgement means context delivery, not spoken playback. Heard-word latency uses independent ASR and voiced carrier PCM, with approximate timing.",
    }
    result["passed"] = all(result["assertions"].values())
    (directory / "latency-breakdown.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    result = measure(parser.parse_args().directory)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
