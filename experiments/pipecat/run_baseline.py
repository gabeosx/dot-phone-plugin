"""Run the real Pipecat adapter and eval harness against a scripted local provider."""

import argparse
import asyncio
import contextlib
import json
import socket
import tempfile
import wave
from pathlib import Path

import numpy as np
import websockets
from loguru import logger
from pipecat.evals.scenario import EvalScenarioFile
from pipecat.evals.session import EvalSession, EvalSessionParams

from bot import Evidence, build_bot
from fake_live import FakeLive


def unused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def fixture_scenario(directory):
    assets = directory / "assets"
    assets.mkdir()
    for i, name in enumerate(["story", "interrupt", "hangup"], 1):
        with wave.open(str(assets / (name + ".wav")), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(24000)
            audio.writeframes(np.full(2400, i * 1000, dtype="<i2").tobytes())
    scenario = directory / "baseline.yaml"
    scenario.write_text(Path(__file__).with_name("baseline.yaml").read_text())
    return EvalScenarioFile.load(scenario).scenarios[0]


async def conversation(scenario, *, key, base_url, evidence, cancel=False, timeout=30):
    bot_port = unused_port()
    run_bot, stop_bot, _ = build_bot(key=key, base_url=base_url, evidence=evidence, port=bot_port)
    task = asyncio.create_task(run_bot())
    session = EvalSession.from_scenario(
        scenario,
        f"ws://127.0.0.1:{bot_port}",
        params=EvalSessionParams(default_timeout_ms=5000, stop_bot=cancel),
    )

    @session.event_handler("on_progress")
    async def progress(session, record):
        if record.status == "turn":
            evidence.mark("turn_sent", turn_index=record.turn_index)

    try:
        return await asyncio.wait_for(session.run(), timeout=timeout)
    finally:

        async def finish():
            if not task.done() and not cancel:
                await stop_bot()
            await task

        try:
            await asyncio.wait_for(finish(), 20)
        except TimeoutError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def run(
    *,
    omit_closed=False,
    omit_usage=False,
    cancel=False,
    start_delay=0,
    end_completion_delay=0,
    close_delay=0,
):
    evidence = Evidence()
    fake = FakeLive(
        omit_closed=omit_closed,
        omit_usage=omit_usage,
        start_delay=start_delay,
        end_completion_delay=end_completion_delay,
        close_delay=close_delay,
    )
    async with websockets.serve(fake.handle, "127.0.0.1", 0) as provider:
        port = provider.sockets[0].getsockname()[1]
        with tempfile.TemporaryDirectory() as temp:
            result = await conversation(
                fixture_scenario(Path(temp)),
                key="offline-fixture",
                base_url=f"ws://127.0.0.1:{port}",
                evidence=evidence,
                cancel=cancel,
            )
    report = evidence.report()
    report.update(
        mode="offline_protocol_fixture",
        pipecat_version="1.12.0",
        eval_passed=result.passed,
        eval_failures=[str(f) for f in result.failures],
        fixture_interrupted_while_speaking=fake.interrupted,
        fixture_post_close_events=fake.post_close_events,
        provider_requests=0,
        pstn_calls=0,
        hard_assertions={
            "tool_lookup": {"name": "lookup_hours", "place": "museum"} in evidence.calls,
            "hangup_tool": {"name": "end_call"} in evidence.calls,
            "interruption_fixture": fake.interrupted,
            "close_requested": evidence.close_sent,
            "provider_closed": evidence.closed,
            "final_usage_confirmed": evidence.final_seconds is not None,
            "no_provider_errors": not evidence.errors,
            "no_client_events_after_close": not fake.post_close_events,
        },
    )
    report["baseline_passed"] = result.passed and all(report["hard_assertions"].values())
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="/artifacts/pipecat-offline.json")
    args = parser.parse_args()
    logger.remove()
    logger.add(lambda m: print(m, end=""), level="WARNING")
    report = asyncio.run(run())
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2))
    print(
        json.dumps(
            {
                "mode": report["mode"],
                "passed": report["baseline_passed"],
                "hard_assertions": report["hard_assertions"],
            }
        )
    )
    raise SystemExit(0 if report["baseline_passed"] else 1)


if __name__ == "__main__":
    main()
