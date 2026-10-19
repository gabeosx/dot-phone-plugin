"""Bounded, owner-approved two-participant GPT-Live evaluations; never PSTN."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import time
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from dots_phone.service import Refused, digest
from dots_phone.store import Store

MAX_RUNS = 16
RUN_WALL_SECONDS = 110
RESERVATION_USD = Decimal("0.25")


def scope(cases):
    return {
        "layer": "B_audio",
        "providers": ["openai"],
        "models": ["gpt-live-1"],
        "participants_per_run": 2,
        "max_runs": MAX_RUNS,
        "max_combined_seconds": 3600,
        "budget_usd": "4.00",
        "wall_seconds_per_run": RUN_WALL_SECONDS,
        "pstn_calls": 0,
        "backend_requests": 0,
        "tts_requests": 0,
        "scenario_hash": digest(cases),
        "scenarios": [c["id"] for c in cases],
    }


def approve(store, owner, cases):
    campaign = {
        **scope(cases),
        "id": uuid4().hex,
        "owner": owner,
        "expires": time.time() + 21600,
        "runs": 0,
        "seconds_reserved": 0,
        "reserved_usd": "0",
        "revoked": False,
    }
    store.put("audio_campaign", campaign["id"], owner, campaign)
    return campaign["id"]


def reserve(store, owner, campaign_id, case, cases):
    with store.transaction():
        campaign = store.get("audio_campaign", campaign_id, owner)
        if not campaign or campaign["revoked"] or campaign["expires"] <= time.time():
            raise Refused("audio campaign unavailable or expired")
        if (
            campaign["scenario_hash"] != digest(cases)
            or case["id"] not in campaign["scenarios"]
            or not case["brief"]["synthetic"]
        ):
            raise Refused("scenario outside approved campaign")
        seconds = 2 * RUN_WALL_SECONDS
        cost = Decimal(campaign["reserved_usd"]) + RESERVATION_USD
        if (
            campaign["runs"] + 1 > campaign["max_runs"]
            or campaign["seconds_reserved"] + seconds > campaign["max_combined_seconds"]
            or cost > Decimal(campaign["budget_usd"])
        ):
            raise Refused("audio campaign exhausted")
        campaign.update(
            runs=campaign["runs"] + 1,
            seconds_reserved=campaign["seconds_reserved"] + seconds,
            reserved_usd=str(cost),
        )
        store.put("audio_campaign", campaign_id, owner, campaign)
        run = {
            "id": uuid4().hex,
            "campaign": campaign_id,
            "owner": owner,
            "scenario": case["id"],
            "created": time.time(),
            "state": "reserved",
            "reservation_usd": str(RESERVATION_USD),
        }
        store.put("audio_run", run["id"], owner, run)
        return run


async def run(store, owner, campaign_id, cases, key, directory, selected=None):
    from dots_phone.harness import evaluate_audio_case

    outputs = []
    # Validate writable storage before reserving a run or opening any provider
    # session. The image filesystem is read-only; /data is a named volume.
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    probe = directory / (".write-probe-" + uuid4().hex)
    probe.write_bytes(b"")
    probe.unlink()
    # A restarted runner never repeats an ambiguous run. Unknown usage freezes
    # the campaign pending reconciliation instead of refunding its reservation.
    if any(
        r["campaign"] == campaign_id and r["state"] in {"reserved", "unconfirmed"}
        for r in store.all("audio_run", owner)
    ):
        raise Refused("prior audio run requires usage reconciliation")
    for case in cases:
        if selected and case["id"] != selected:
            continue
        reservation = reserve(store, owner, campaign_id, case, cases)
        destination = directory / reservation["id"]
        try:
            result = await evaluate_audio_case(
                case, key, destination, delay=case.get("supervisor_delay_seconds", 0)
            )
            result["campaign_id"], result["run_id"] = campaign_id, reservation["id"]
            result["scenario_sha256"] = hashlib.sha256(
                json.dumps(case, sort_keys=True).encode()
            ).hexdigest()
            destination.joinpath("result.json").write_text(json.dumps(result, indent=2))
            confirmed = result["hard_assertions"]["both_voice_usage_confirmed"]
            reservation.update(
                state="completed" if confirmed else "unconfirmed",
                result_path=str(destination / "result.json"),
                usage=result["usage_accounting"],
            )
            store.put("audio_run", reservation["id"], owner, reservation)
            outputs.append(
                {
                    "scenario": case["id"],
                    "passed": result["audio_gate"]["full_acceptance_passed"],
                    "result": str(destination / "result.json"),
                }
            )
            print(json.dumps(outputs[-1]), flush=True)
            if not confirmed:
                raise Refused("voice usage unconfirmed; campaign stopped")
        except BaseException as exc:
            campaign = store.get("audio_campaign", campaign_id, owner)
            campaign["revoked"] = True
            store.put("audio_campaign", campaign_id, owner, campaign)
            # Never leave an ambiguous failure labeled only 'reserved'. Keep
            # its full reservation and safe recovery information across restart.
            if reservation["state"] == "reserved":
                reservation.update(state="unconfirmed", failure_type=type(exc).__name__)
                store.put("audio_run", reservation["id"], owner, reservation)
                try:
                    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
                    destination.joinpath("failure.json").write_text(
                        json.dumps(
                            {
                                "scenario_id": case["id"],
                                "campaign_id": campaign_id,
                                "run_id": reservation["id"],
                                "state": "unconfirmed",
                                "failure_type": type(exc).__name__,
                                "reservation_retained": True,
                                "provider_details_suppressed": True,
                            },
                            indent=2,
                        )
                    )
                except OSError:
                    pass  # The encrypted ledger remains authoritative if disk is full.
            raise
    return outputs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign_id")
    parser.add_argument("--case")
    parser.add_argument("--artifacts", default="/data/audio")
    args = parser.parse_args()
    from dots_phone.bws import read_private
    from dots_phone.harness import load_cases

    try:
        values = json.loads(read_private("/run/secrets/runtime"))
        store = Store(
            Path(os.getenv("DOTS_PHONE_DATABASE", "/data/phone.sqlite3")).with_name(
                "audio-campaign.sqlite3"
            ),
            values["DOTS_PHONE_DATA_ENCRYPTION_KEY"],
        )
        try:
            outputs = asyncio.run(
                run(
                    store,
                    os.getenv("DOTS_PHONE_ALLOWED_USER_SUBJECT", "owner"),
                    args.campaign_id,
                    load_cases(),
                    values["OPENAI_API_KEY"],
                    Path(args.artifacts),
                    args.case,
                )
            )
        finally:
            store.close()
        raise SystemExit(0 if outputs and all(x["passed"] for x in outputs) else 1)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "BLOCKED_OR_FAILED",
                    "reason": str(exc) if isinstance(exc, Refused) else type(exc).__name__,
                    "provider_details_suppressed": True,
                }
            ),
            flush=True,
        )
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
