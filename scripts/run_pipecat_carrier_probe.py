"""One bounded real carrier call between exact account-owned synthetic endpoints."""

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from twilio.http.http_client import TwilioHttpClient
from twilio.rest import Client

from dots_phone.bws import read_private
from dots_phone.twilio_security import fetch_exact_call

TERMINAL = {"completed", "canceled", "failed", "busy", "no-answer"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--caller", required=True)
    parser.add_argument("--number", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.caller == args.number or not args.origin.startswith("https://"):
        raise SystemExit("Two different owned numbers and an HTTPS origin are required")
    values = json.loads(read_private(".secrets/pipecat-inbound-runtime.json"))
    admin = Client(
        values["TWILIO_ACCOUNT_SID"],
        values["TWILIO_AUTH_TOKEN"],
        http_client=TwilioHttpClient(timeout=10, max_retries=0),
    )
    account = admin.api.accounts(values["TWILIO_ACCOUNT_SID"]).fetch()
    if account.type != "Full" or account.status != "active":
        raise SystemExit("An active Full account is required")
    owned = {n.phone_number for n in admin.incoming_phone_numbers.list(limit=20)}
    if not {args.caller, args.number} <= owned:
        raise SystemExit("Both endpoints must be present in the account's number inventory")
    health_response = httpx.get(args.origin + "/healthz", timeout=15)
    if health_response.status_code != 200:
        raise SystemExit("Public receiver is not ready; no call was placed")
    health = health_response.json()
    if not health.get("phone_streaming_enabled") or not health.get("owned_bot_enabled"):
        raise SystemExit("Both the assistant and synthetic caller must be enabled")
    args.output.mkdir(mode=0o700)
    result_path = args.output / "carrier-result.json"
    result = {
        "mode": "account_owned_synthetic_pstn_call",
        "started_at": datetime.now(UTC).isoformat(),
        "caller": args.caller,
        "number": args.number,
        "state": "prepared",
        "max_seconds": 90,
        "automatic_retry": False,
        "human_called": False,
        "carrier_recording_enabled": False,
        "local_synthetic_audio_capture": True,
    }

    def save():
        result_path.write_text(json.dumps(result, indent=2) + "\n")

    client = Client(
        values["TWILIO_API_KEY"],
        values["TWILIO_API_SECRET"],
        account_sid=values["TWILIO_ACCOUNT_SID"],
        http_client=TwilioHttpClient(timeout=10, max_retries=0),
    )
    sid = None
    save()
    try:
        call = client.calls.create(
            from_=args.caller,
            to=args.number,
            url=args.origin + "/twilio/owned-bot",
            method="POST",
            time_limit=90,
            timeout=20,
            record=False,
        )
        sid = call.sid
        result.update(state="dialed", call_sid=sid)
        save()
        deadline = time.monotonic() + 105
        while time.monotonic() < deadline:
            remote = fetch_exact_call(admin, sid)
            if remote.status in TERMINAL:
                result.update(state="carrier_terminated", carrier_status=remote.status)
                break
            time.sleep(2)
        else:
            result.update(state="deadline", carrier_status=remote.status)
    except Exception as error:
        result.update(state="failed", error_type=type(error).__name__)
        if hasattr(error, "code"):
            result["provider_code"] = error.code
    finally:
        if sid:
            try:
                remote = fetch_exact_call(admin, sid)
                if remote.status not in TERMINAL:
                    client.calls(sid).update(status="completed")
                recent = admin.calls.list(from_=args.caller, to=args.number, limit=20)
                matching = [
                    c
                    for c in recent
                    if c.sid == sid
                    or (
                        c.direction == "inbound"
                        and c.date_created
                        and c.date_created.timestamp()
                        >= int(datetime.fromisoformat(result["started_at"]).timestamp())
                    )
                ]
                for c in matching:
                    if c.status not in TERMINAL:
                        client.calls(c.sid).update(status="completed")
                pricing_deadline = time.monotonic() + 25
                while True:
                    final = [fetch_exact_call(admin, c.sid) for c in matching]
                    if (
                        all(c.price is not None for c in final)
                        or time.monotonic() >= pricing_deadline
                    ):
                        break
                    time.sleep(3)
                result["carrier_legs"] = [
                    {
                        "call_sid": c.sid,
                        "direction": c.direction,
                        "status": c.status,
                        "duration_seconds": c.duration,
                        "price": c.price,
                        "price_unit": c.price_unit,
                    }
                    for c in final
                ]
                result["carrier_teardown_confirmed"] = bool(final) and all(
                    c.status in TERMINAL for c in final
                )
                result["carrier_prices_confirmed"] = bool(final) and all(
                    c.price is not None for c in final
                )
            except Exception:
                result["carrier_teardown_confirmed"] = False
        time.sleep(2)
        for name in ["inbound-demo.json", "owned-bot-status.json"]:
            source = args.output / name
            if source.exists():
                result[name] = json.loads(source.read_text())
        # The assistant's status file is shared, but only this run's inbound call is retained.
        shared = Path("artifacts/pipecat/inbound-demo.json")
        if shared.exists() and sid:
            data = json.loads(shared.read_text())
            inbound_ids = {
                c["call_sid"] for c in result.get("carrier_legs", []) if c["direction"] == "inbound"
            }
            result["assistant"] = [
                c for c in data.get("calls", []) if c.get("call_sid") in inbound_ids
            ]
        caller = result.get("owned-bot-status.json", {}).get("calls", {}).get(sid, {})
        assistants = result.get("assistant", [])
        result["assertions"] = {
            "two_carrier_legs_completed": len(result.get("carrier_legs", [])) == 2
            and all(c["status"] == "completed" for c in result["carrier_legs"]),
            "synthetic_caller_completed": caller.get("state") == "completed",
            "speech_at_interruption": caller.get("voice_at_interruption") is True,
            "caller_received_stop": caller.get("stream_stopped") is True,
            "assistant_closed_with_usage": len(assistants) == 1
            and assistants[0].get("model_closed") is True
            and (
                assistants[0].get("live_seconds") is not None
                or assistants[0].get("model_usage", {}).get("engine") == "realtime"
                and assistants[0]["model_usage"].get("usage_confirmed") is True
            ),
            "lookup_and_hangup_tools": len(assistants) == 1
            and {"lookup_hours", "end_call"} <= set(assistants[0].get("tools") or []),
            "no_model_errors": len(assistants) == 1
            and assistants[0].get("provider_error_count") == 0,
            "realtime_tool_outputs_sent_before_close": len(assistants) == 1
            and (
                assistants[0].get("model_usage", {}).get("engine") != "realtime"
                or assistants[0]["model_usage"].get("tool_outputs_transmitted") is True
            ),
            "carrier_buffers_cleared": len(assistants) == 1
            and assistants[0].get("carrier_audio_clears", 0) >= 3,
        }
        result["protocol_passed"] = all(result["assertions"].values())
        save()
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result.get("protocol_passed") else 1)


if __name__ == "__main__":
    main()
