"""Complete no-PSTN conversation through the production Pipecat source adapter.

Uses the existing voice API and a ChatGPT subscription source worker. The
counterparty is prerecorded; source queries and interpretation are chosen by
the voice model. The initial packet has no accommodation deadlines or mail IDs.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import hashlib
import json
import os
import time
import wave
from datetime import timedelta
from pathlib import Path

import websockets
import yaml
from dots_phone.bws import read_private
from dots_phone.config import Settings
from dots_phone.local_call_mail_worker import (
    INFORMATION_ACTIONS,
    INFORMATION_CONSTRAINT,
    LocalCallMailWorker,
    call_mail_instructions,
    call_mail_tools,
)
from dots_phone.local_gmail_worker import private_json
from dots_phone.models import Brief, now
from dots_phone.pipecat_realtime import SupervisedRealtimeService
from dots_phone.prepared_evidence import evidence_digest, prepared_instructions
from dots_phone.service import PhoneService
from dots_phone.store import Store
from loguru import logger
from pipecat.evals.scenario import EvalScenarioFile
from pipecat.evals.serializer import EvalSerializer
from pipecat.evals.session import EvalSession, EvalSessionParams
from pipecat.evals.transport import EvalTransport, EvalTransportParams
from pipecat.frames.frames import EndFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.services.llm_service import FunctionCallParams
from pipecat.services.openai.realtime import events
from pipecat.workers.runner import WorkerRunner

from audio_metrics import AudioCapture, pcm_fields
from bot import Evidence
from run_baseline import unused_port

LINES = [
    "Hello, this is the event planning desk. What can I help with?",
    "What time does the briefing start, and where should attendees meet?",
    "One more thing: could you check the ScentFest planning email in Gmail? Which of the two accommodation offers expires later, what are both dates, and how many days apart are they?",
    "While you're checking that, is the meeting point indoors or outside?",
    "Could you repeat both accommodation deadlines slowly, with the hotel names?",
    "Actually, stop repeating the dates. Just tell me the difference in days once more.",
    "Yes, I've noted both accommodation deadlines.",
    "Thanks, end this call now.",
]


def write(path, value):
    private_json(path, value)


async def generate_clips(key, directory):
    assets = directory / "assets"
    assets.mkdir(mode=0o700, exist_ok=True)
    for index, line in enumerate(LINES):
        destination = assets / f"{index}.wav"
        if destination.exists():
            continue
        pcm, usage, transcript = bytearray(), None, ""
        created, done = set(), set()
        try:
            async with asyncio.timeout(35):
                async with websockets.connect(
                    "wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1",
                    additional_headers={"Authorization": "Bearer " + key},
                ) as socket:
                    await socket.send(
                        json.dumps(
                            {
                                "type": "session.update",
                                "session": {
                                    "type": "realtime",
                                    "model": "gpt-realtime-2.1",
                                    "output_modalities": ["audio"],
                                    "reasoning": {"effort": "minimal"},
                                    "audio": {"output": {"voice": "cedar"}},
                                    "tools": [],
                                    "instructions": "Read the supplied line exactly as a natural phone caller. Speak only that line, without answering it.",
                                    "max_output_tokens": 512,
                                },
                            }
                        )
                    )
                    async for raw in socket:
                        event = json.loads(raw)
                        if event["type"] == "session.updated":
                            await socket.send(
                                json.dumps(
                                    {
                                        "type": "conversation.item.create",
                                        "item": {
                                            "type": "message",
                                            "role": "user",
                                            "content": [{"type": "input_text", "text": line}],
                                        },
                                    }
                                )
                            )
                            await socket.send(json.dumps({"type": "response.create"}))
                        elif event["type"] == "response.created":
                            created.add(event["response"]["id"])
                        elif event["type"] == "response.output_audio.delta":
                            pcm.extend(base64.b64decode(event["delta"]))
                        elif event["type"] == "response.output_audio_transcript.done":
                            transcript = event["transcript"]
                        elif event["type"] == "response.done":
                            done.add(event["response"]["id"])
                            usage = event["response"].get("usage")
                            if event["response"]["status"] != "completed":
                                raise RuntimeError("fixture_incomplete")
                            break
                        elif event["type"] == "error":
                            raise RuntimeError(event["error"].get("code", "fixture_error"))
            if not pcm or not usage or created != done:
                raise RuntimeError("fixture_usage_missing")
        finally:
            write(
                assets / f"{index}-usage.json",
                {
                    "usage": usage,
                    "created": sorted(created),
                    "done": sorted(done),
                    "transcript": transcript,
                    "socket_closed": True,
                },
            )
        with wave.open(str(destination), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(24000)
            stream.writeframes(pcm)
        destination.chmod(0o600)
        print(
            json.dumps({"fixture": index, "seconds": len(pcm) / 48000, "usage_confirmed": True}),
            flush=True,
        )


class AudioProvider:
    """No carrier implementation: even an accidental dial is rejected."""

    def __init__(self):
        self.llm = None
        self.stopped = asyncio.Event()

    async def dial(self, call):
        raise RuntimeError("no_phone_provider_in_audio_fixture")

    async def update(self, call, delegation, text):
        await self.llm.deliver_decision(call, delegation, text)

    async def stop(self, call):
        self.stopped.set()

    async def close_voice(self, call):
        pass


class CapturedRealtime(SupervisedRealtimeService):
    def __init__(self, *, evidence, **kwargs):
        self.evidence = evidence
        super().__init__(**kwargs)

    async def observe(self, event):
        kind = event["type"]
        if kind in {"response.output_audio.delta", "response.audio.delta"}:
            pcm = base64.b64decode(event["delta"])
            self.evidence.mark(
                "output_audio",
                response_id=event.get("response_id"),
                **pcm_fields(pcm),
                **self.evidence.capture.append("output", pcm),
            )
        elif kind in {"input_audio_buffer.speech_started", "input_audio_buffer.speech_stopped"}:
            self.evidence.mark("caller_boundary", event_type=kind, item_id=event.get("item_id"))
        elif kind in {
            "conversation.item.input_audio_transcription.completed",
            "response.output_audio_transcript.done",
        }:
            self.evidence.mark(
                "caption",
                role="user" if kind.startswith("conversation") else "assistant",
                text=event.get("transcript", ""),
            )
        elif kind == "response.done":
            self.evidence.mark(
                "response_done",
                response_id=event["response"]["id"],
                status=event["response"]["status"],
                usage=event["response"].get("usage"),
                error_type=((event["response"].get("status_details") or {}).get("error") or {}).get(
                    "type"
                ),
                error_code=((event["response"].get("status_details") or {}).get("error") or {}).get(
                    "code"
                ),
            )
        elif kind == "error":
            self.evidence.errors.append({"code": event["error"].get("code")})
        elif kind == "response.function_call_arguments.done":
            self.evidence.mark(
                "tool_arguments", name=event.get("name"), arguments=event.get("arguments")
            )
        await super().observe(event)

    async def send_client_event(self, event):
        value = event.model_dump(exclude_none=True)
        if value["type"] == "input_audio_buffer.append":
            pcm = base64.b64decode(value["audio"])
            self.evidence.mark(
                "input_audio", **pcm_fields(pcm), **self.evidence.capture.append("input", pcm)
            )
        elif value["type"] in {"response.create", "conversation.item.create"}:
            self.evidence.mark(
                "backend_command",
                event_type=value["type"],
                item_type=value.get("item", {}).get("type"),
                output=value.get("item", {}).get("output")
                if value["type"] == "conversation.item.create"
                else None,
            )
        await super().send_client_event(event)


def scenario(directory, assets, *, delayed):
    def spoken(index, expect, **other):
        return {
            "user": LINES[index],
            "audio": str(assets / f"{index}.wav"),
            "expect": expect,
            **other,
        }

    def response(text=""):
        # Caption events pace the script; the separate offline acoustic audit
        # grades actual speech. Audio mode is needed for client-side recording.
        return {"event": "llm_response", "text_contains": text, "within_ms": 45000}

    turns = [
        spoken(0, [response()]),
        spoken(1, [response("lobby")]),
        spoken(
            2,
            [{"event": "function_call", "calls": [{"name": "read_call_mail"}], "within_ms": 45000}],
        ),
        spoken(3, [response("lobby")], send_after={"delay_ms": 1000}),
        {"expect": [response("June")]},
        spoken(4, [{"event": "bot_started_speaking", "within_ms": 15000}]),
        spoken(
            5,
            [{"event": "bot_interrupted", "within_ms": 10000}, response("days")],
            send_after={"delay_ms": 700},
        ),
        spoken(
            6,
            [
                {
                    "event": "function_call",
                    "calls": [{"name": "record_information_confirmation"}],
                    "within_ms": 15000,
                },
                response(),
            ],
        ),
        spoken(7, [{"event": "user_transcription", "text_contains": "call", "within_ms": 15000}]),
    ]
    if not delayed:
        # Ordinary conversation: wait for the useful answer. Pending-lookup
        # responsiveness is measured separately by the deliberate-delay run.
        turns[2]["expect"].append(response("June"))
        del turns[3:5]
    path = directory / "scenario.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "name": "information_call",
                "user": {"modality": "audio"},
                "judge": {
                    "modality": "audio",
                    "transcription": {"service": "whisper", "model": "small.en"},
                },
                "scenarios": [{"name": "prepared_live_mail_interruption", "turns": turns}],
            }
        )
    )
    path.chmod(0o600)
    return EvalScenarioFile.load(path).scenarios[0]


async def run(key, root, policy, delay):
    output = root / ("run-" + str(time.time_ns()))
    output.mkdir(mode=0o700)
    bridge = output / "bridge"
    bridge.mkdir(mode=0o700)
    settings = Settings(
        profile="evaluation",
        database=output / "state.db",
        allowed_numbers=["+12125550100"],
        decision_seconds=45,
        trace_realtime_stages=True,
        retain_transcript=True,
    )
    provider = AudioProvider()
    store = Store(settings.database, settings.encryption_key.get_secret_value())
    service = PhoneService(settings, store, provider)
    service.ready = True
    source = {
        "provider": "owned_fixture",
        "source_id": "event-brief",
        "text": "The fictional event's morning briefing begins at 10 AM. Attendees gather indoors in the west lobby. This note contains no accommodation deadlines.",
        "truncated": False,
    }
    source["source_sha256"] = hashlib.sha256(source["text"].encode()).hexdigest()
    facts = [
        {
            "name": name,
            "value": value,
            "provenance": "owner-approved information mock",
            "verified_at": now(),
            "expires_at": now() + timedelta(minutes=15),
        }
        for name, value in [
            ("prepared_evidence_digest", evidence_digest([source])),
            ("information_confirmation_phrase", "I've noted both accommodation deadlines"),
        ]
    ]
    brief = Brief(
        destination="+12125550100",
        purpose="Give the owned mock event desk its attendee briefing details and answer planning questions. Historical ScentFest accommodation offers may be discussed as an information example; no real booking or event is changed.",
        facts=facts,
        disclosures=[f["name"] for f in facts],
        actions=sorted(INFORMATION_ACTIONS),
        constraints=[
            INFORMATION_CONSTRAINT,
            "Discuss only event-note facts and the scoped historical ScentFest accommodation offers.",
        ],
        deadline=now() + timedelta(minutes=10),
        max_seconds=180,
        budget_usd="3",
        synthetic=True,
    )
    cid = service.prepare("owner", brief)["call_id"]
    call = service.call("owner", cid)
    # Simulate connected audio only. Never approve, start, or invoke a carrier.
    call.update(state="active", provider_sid="owned-audio-fixture")
    service.save(call)
    mail_policy = json.loads(read_private(policy))
    write(
        bridge / "registry.json",
        {
            "schema": 5,
            "cases": [
                {
                    "call_id": cid,
                    "revision": 1,
                    "brief_hash": call["brief_hash"],
                    "provider": "connected_trip_mail",
                    "max_requests": 10,
                    "mail_scope": {"gmail": mail_policy["mail_scope"]["gmail"]},
                }
            ],
        },
    )
    write(
        bridge / (cid + "-prepared.json"),
        {
            "schema": 1,
            "call_id": cid,
            "revision": 1,
            "brief_hash": call["brief_hash"],
            "expires": time.time() + 500,
            "sources": [source],
        },
    )
    service.local_context_worker = LocalCallMailWorker(service, bridge)
    prompt = call_mail_instructions(call) + prepared_instructions(call, bridge)
    write(
        output / "session-scope.json",
        {
            "mode": "production_pipecat_no_dial",
            "prompt": prompt,
            "call_id": cid,
            "brief_hash": call["brief_hash"],
            "injected_read_delay_ms": delay,
            "phone_calls": 0,
            "additional_text_api_calls": 0,
        },
    )
    evidence = Evidence()
    evidence.capture = AudioCapture(output / "audio")
    port = unused_port()
    transport = EvalTransport(
        host="127.0.0.1",
        port=port,
        params=EvalTransportParams(
            audio_in_enabled=True, audio_out_enabled=True, serializer=EvalSerializer()
        ),
    )
    llm = CapturedRealtime(
        evidence=evidence,
        supervisor=service,
        call_id=cid,
        api_key=key,
        settings=SupervisedRealtimeService.Settings(
            model="gpt-realtime-2.1",
            system_instruction=prompt,
            session_properties=events.SessionProperties(
                output_modalities=["audio"],
                max_output_tokens=512,
                reasoning=events.Reasoning(effort="minimal"),
                audio=events.AudioConfiguration(
                    input=events.AudioInput(
                        transcription=events.InputAudioTranscription(language="en"),
                        turn_detection=events.TurnDetection(silence_duration_ms=1000),
                    ),
                    output=events.AudioOutput(voice="marin"),
                ),
            ),
        ),
    )
    provider.llm = llm

    async def hang_up(params: FunctionCallParams, recipient_request: str):
        """End the call only on the recipient's exact request to end it."""
        await params.result_callback({"ending": llm.request_stop(recipient_request)})

    context = LLMContext([], tools=call_mail_tools(llm, hang_up))
    llm.prepare_listen_first(context)
    user, assistant = LLMContextAggregatorPair(context)
    worker = PipelineWorker(
        Pipeline([transport.input(), user, llm, transport.output(), assistant]),
        params=PipelineParams(audio_in_sample_rate=24000, audio_out_sample_rate=24000),
        idle_timeout_secs=180,
    )
    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    node = None
    task = None
    result = None
    failure = None
    try:
        node = await asyncio.create_subprocess_exec(
            "node",
            "experiments/pipecat/information_source_worker.mjs",
            str(bridge),
            mail_policy["gmail_account"],
            str(delay),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        ready = json.loads(await asyncio.wait_for(node.stdout.readline(), 45))
        if ready.get("ready") is not True:
            raise RuntimeError("source_worker_not_ready")
        task = asyncio.create_task(runner.run())
        session = EvalSession.from_scenario(
            scenario(output, root / "assets", delayed=delay > 0),
            f"ws://127.0.0.1:{port}",
            params=EvalSessionParams(
                default_timeout_ms=45000, record_path=str(output / "complete-conversation.wav")
            ),
        )
        result = await asyncio.wait_for(session.run(), 180)
        if result.passed:
            await asyncio.wait_for(provider.stopped.wait(), 10)
    except Exception as exc:
        failure = type(exc).__name__
    finally:
        if task and not task.done():
            await worker.queue_frames([EndFrame()])
            try:
                await asyncio.wait_for(task, 10)
            except TimeoutError:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        (bridge / "finished.signal").touch(mode=0o600)
        if node:
            try:
                await asyncio.wait_for(node.wait(), 5)
            except TimeoutError:
                node.terminate()
                await node.wait()
            stderr = await node.stderr.read()
            if stderr:
                (output / "source-worker-errors.log").write_bytes(stderr)
        evidence.capture.close()
        current = service.call("owner", cid)
        write(
            output / "result.json",
            {
                "failure": failure,
                "scenario_passed": result.passed if result else False,
                "scenario": {
                    "failures": [str(f) for f in result.failures],
                    "events": result.events_seen,
                    "debug_log": result.debug_log,
                }
                if result
                else None,
                "call": current,
                "timeline": evidence.timeline,
                "errors": evidence.errors,
                "voice_usage_confirmed": current["usage"].get("realtime_usage_confirmed", False),
                "voice_closed": llm.closed.is_set(),
                "recipient_stop_observed": provider.stopped.is_set(),
                "phone_calls": 0,
            },
        )
        for path in output.rglob("*"):
            if path.is_file():
                path.chmod(0o600)
            elif path.is_dir():
                path.chmod(0o700)
        store.close()
    print(
        json.dumps(
            {
                "output": str(output),
                "scenario_passed": result.passed if result else False,
                "failure": failure,
                "voice_closed": llm.closed.is_set(),
                "usage_confirmed": current["usage"].get("realtime_usage_confirmed", False),
            }
        ),
        flush=True,
    )


async def main(args):
    os.umask(0o077)
    logger.remove()
    logger.add(lambda _: None, level="ERROR")
    args.output.mkdir(parents=True, mode=0o700, exist_ok=True)
    key = json.loads(read_private(args.runtime))["OPENAI_API_KEY"]
    if args.generate:
        await generate_clips(key, args.output)
    else:
        await run(key, args.output.resolve(), args.policy, args.delay_ms)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--generate", action="store_true")
    parser.add_argument("--delay-ms", type=int, choices=range(0, 12001), default=12000)
    asyncio.run(main(parser.parse_args()))
