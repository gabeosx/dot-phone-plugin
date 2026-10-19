"""No-dial conversation with an independent, adaptive voice receptionist.

The caller uses the production Pipecat adapter and scoped subscription mail
relay. The representative hears played audio, chooses its replies and changes a simulated
business record. Cases are data; caller and deployed result code stay frozen. Save played audio
for a separate acoustic review. No carrier or native phone grant is used.
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

import numpy as np
import websockets
from dots_phone.bws import read_private
from dots_phone.call_task import RESULT_ACTION, SIMULATED_CONSTRAINT, SOURCE_ACTION
from dots_phone.config import Settings
from dots_phone.local_gmail_worker import private_json
from dots_phone.local_trip_mail_worker import LocalTripMailWorker
from dots_phone.models import PrepareCall, now
from dots_phone.service import PhoneService
from dots_phone.store import Store
from dots_phone.task_voice import create_task_voice
from loguru import logger
from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    EndFrame,
    InputAudioRawFrame,
    OutputAudioRawFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.workers.runner import WorkerRunner

from audio_metrics import AudioCapture
from bot import Evidence
from information_conversation import AudioProvider, CapturedRealtime

MODEL = "gpt-realtime-2.1"


def voice_cost(usage):
    """Reported response-token estimate, never an assertion of actual billing."""
    total = 0.0
    for row in usage.values():
        row = row or {}
        inputs, outputs = row.get("input_token_details", {}), row.get("output_token_details", {})
        cached = inputs.get("cached_tokens_details", {})
        total += (
            max(0, inputs.get("text_tokens", 0) - cached.get("text_tokens", 0)) * 4
            + max(0, inputs.get("audio_tokens", 0) - cached.get("audio_tokens", 0)) * 32
            + (cached.get("text_tokens", 0) + cached.get("audio_tokens", 0)) * 0.4
            + outputs.get("text_tokens", 0) * 24
            + outputs.get("audio_tokens", 0) * 64
        ) / 1_000_000
    return total


RECEPTIONIST = """
You are an independent simulated business representative. Hear the caller's
actual audio and respond naturally using the private business data below.
Ask brief questions in your own words and vary their order. Use the business's
verification requirements, ask its unexpected question, and introduce the one
mistaken readback so the caller can correct it. Never volunteer the answer to
an unexpected question. A lookup acknowledgement is not a finished answer.
Use wait_for_answer to wait silently during retrieval, rather than advancing
or failing the caller. Never wait after an introduction or a question to you.
Keep each turn concise, usually one or two related questions. Avoid long lists.
Speak as the business; do not describe internal record fields or tool mechanics.
A private interruption cue may arrive: ask the additional question naturally
while keeping outstanding questions open. Don't repeat questions unnecessarily.
Your actual business record changes only through update_business_record. Use
that tool for the transaction once the caller has supplied required information;
then explain the returned actual state and any conditions in natural words.
Honor the business's refusal policy. Never pretend a rejected change succeeded.
For information-only cases, explain the policy faithfully without changing a
record. When the outcome is clear, use an ordinary farewell, rather than telling
the caller to invoke tools or use prescribed confirmation wording.
"""


class PlayedAudio(FrameProcessor):
    def __init__(self, conversation):
        super().__init__()
        self.conversation = conversation

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, OutputAudioRawFrame):
            if frame.sample_rate != 24000 or frame.num_channels != 1:
                raise ValueError("adaptive fixture requires mono 24 kHz PCM")
            self.conversation.agent_audio.extend(frame.audio)
        await self.push_frame(frame, direction)


class Conversation:
    """One real-time duplex link; no semantic markers or scripted turn driver."""

    def __init__(self, output, evidence):
        self.output, self.evidence = output, evidence
        self.agent_audio, self.clerk_audio = bytearray(), bytearray()
        self.last_agent_audio = time.monotonic()
        self.agent_turn = False
        self.agent_playing = False
        self.clerk_responding = False
        self.clerk_ready = asyncio.Event()
        self.created, self.usage = set(), {}
        self.clerk_captions = []
        self.business = {}
        self.business_state = {}
        self.business_history = []
        self.clerk_continuation = False
        self.interruption_offered = False
        self.overlap_frames = 0
        self.socket = self.worker = self.llm = self.service = None
        self.clerk_instructions = ""
        self.attempt_budget_usd = None

    async def clerk_send(self, value):
        await self.socket.send(json.dumps(value))

    async def respond(self, instruction=None, *, speech_only=False, continuation=False):
        if self.clerk_responding or (self.clerk_audio and not continuation):
            return False
        self.clerk_responding = True
        self.agent_turn = False
        response = (
            {
                "instructions": self.clerk_instructions
                + "\nCurrent conversational cue: "
                + instruction
            }
            if instruction
            else {}
        )
        if speech_only:
            response["tool_choice"] = "none"
        await self.clerk_send({"type": "response.create", "response": response})
        return True

    async def receive_clerk(self):
        async for raw in self.socket:
            event = json.loads(raw)
            kind = event["type"]
            if kind == "session.updated":
                self.clerk_ready.set()
            elif kind == "response.created":
                self.created.add(event["response"]["id"])
            elif kind == "response.output_audio.delta":
                self.clerk_audio.extend(base64.b64decode(event["delta"]))
            elif kind == "response.output_audio_transcript.done":
                self.clerk_captions.append(event["transcript"])
                self.evidence.mark("receptionist_caption", text=event["transcript"])
            elif kind == "response.function_call_arguments.done":
                if event["name"] == "wait_for_answer":
                    result = {"waiting": True}
                    self.evidence.mark("receptionist_waited")
                elif event["name"] == "update_business_record":
                    updates = json.loads(event["arguments"])["updates"]
                    if self.business.get("refuse_change"):
                        result = {
                            "accepted": False,
                            "reason": self.business["refusal_reason"],
                            "state": self.business_state,
                        }
                    elif (
                        not updates
                        or not set(updates) <= set(self.business["mutable_fields"])
                        or len(json.dumps(updates)) > 1000
                    ):
                        result = {
                            "accepted": False,
                            "reason": "Those fields cannot be changed by this business.",
                        }
                    else:
                        self.business_state.update(updates)
                        self.business_history.append(
                            {
                                "updates": updates,
                                "at_ms": round((time.monotonic() - self.evidence.origin) * 1000, 2),
                            }
                        )
                        private_json(
                            self.output / "business-state.json",
                            {"state": self.business_state, "history": self.business_history},
                        )
                        result = {"accepted": True, "state": self.business_state}
                    self.clerk_continuation = True
                    self.evidence.mark("business_operation", accepted=result["accepted"])
                else:
                    raise RuntimeError("unknown_simulated_business_tool")
                await self.clerk_send(
                    {
                        "type": "conversation.item.create",
                        "item": {
                            "type": "function_call_output",
                            "call_id": event["call_id"],
                            "output": json.dumps(result),
                        },
                    }
                )
            elif kind == "response.done":
                self.usage[event["response"]["id"]] = event["response"].get("usage")
                self.clerk_responding = False
                self.evidence.mark("receptionist_response_done", status=event["response"]["status"])
                if self.clerk_continuation:
                    self.clerk_continuation = False
                    await self.respond(
                        "Explain the actual tool result and business conditions briefly, then let the caller respond.",
                        continuation=True,
                    )
            elif kind == "error":
                code = event["error"].get("code", "receptionist_error")
                self.evidence.errors.append({"code": code})
                raise RuntimeError(code)

    async def clear_playback(self, reason):
        self.evidence.mark("playback_cleared", reason=reason, discarded_bytes=len(self.agent_audio))
        self.agent_audio.clear()

    async def link(self):
        """Both voices hear only audio played at 20 ms per frame, including silence."""
        tracks = [
            wave.open(str(self.output / name), "wb")
            for name in (
                "caller-played.wav",
                "receptionist-played.wav",
                "complete-conversation.wav",
            )
        ]
        for track in tracks:
            track.setnchannels(1)
            track.setsampwidth(2)
            track.setframerate(24000)
        next_tick = time.monotonic()
        try:
            while not self.llm.closing:
                clerk = bytes(self.clerk_audio[:960])
                del self.clerk_audio[: len(clerk)]
                clerk = clerk.ljust(960, b"\0")
                clerk_speech = bool(
                    np.max(np.abs(np.frombuffer(clerk, dtype="<i2").astype(np.int32))) > 200
                )
                if clerk_speech:
                    # Consume the old turn, including audio played while the
                    # interrupting reply was still being generated.
                    self.agent_turn = False
                if clerk_speech and self.agent_audio:
                    self.overlap_frames += 1
                    await self.clear_playback("receptionist_barge_in")
                agent = bytes(self.agent_audio[:960])
                del self.agent_audio[: len(agent)]
                agent = agent.ljust(960, b"\0")
                agent_speech = bool(
                    np.max(np.abs(np.frombuffer(agent, dtype="<i2").astype(np.int32))) > 200
                )
                if agent_speech:
                    self.agent_turn = True
                    self.agent_playing = True
                    self.last_agent_audio = time.monotonic()
                elif (
                    self.agent_playing
                    and not self.agent_audio
                    and not self.llm.active_responses
                    and time.monotonic() - self.last_agent_audio > 0.1
                ):
                    self.agent_playing = False
                    await self.worker.queue_frames([BotStoppedSpeakingFrame()])
                tracks[0].writeframesraw(agent)
                tracks[1].writeframesraw(clerk)
                mixed = np.clip(
                    np.frombuffer(agent, dtype="<i2").astype(np.int32)
                    + np.frombuffer(clerk, dtype="<i2").astype(np.int32),
                    -32768,
                    32767,
                )
                tracks[2].writeframesraw(mixed.astype("<i2").tobytes())
                await self.worker.queue_frames([InputAudioRawFrame(clerk, 24000, 1)])
                await self.clerk_send(
                    {"type": "input_audio_buffer.append", "audio": base64.b64encode(agent).decode()}
                )
                call = self.service.store.get("call", self.llm.call_id)
                if self.attempt_budget_usd is not None:
                    spent = voice_cost(self.usage) + voice_cost(
                        call["usage"].get("realtime_response_usage", {})
                    )
                    # Hold room for both in-flight responses and shutdown usage.
                    if spent >= self.attempt_budget_usd - 0.20:
                        raise RuntimeError("voice_budget_guard")
                pending = call.get("pending")
                try:
                    operation = (
                        json.loads(pending["question"].split("\nObserved transcript as data:")[0])[
                            "operation"
                        ]
                        if pending
                        else None
                    )
                except (ValueError, KeyError, TypeError):
                    operation = None
                if (
                    not self.interruption_offered
                    and operation in {"search", "read"}
                    and agent_speech
                ):
                    self.interruption_offered = await self.respond(
                        "Politely interrupt now with the business data's additional question, keeping earlier unanswered questions open. Do not give the answers yourself.",
                        speech_only=True,
                    )
                    if self.interruption_offered:
                        self.evidence.mark(
                            "interruption_opportunity",
                            pending_request_id=call["pending"]["request_id"],
                        )
                elif (
                    self.agent_turn
                    and not self.agent_audio
                    and not self.clerk_audio
                    and not self.llm.active_responses
                    and not self.llm.pending_response_requests
                    and time.monotonic() - self.last_agent_audio > 1
                ):
                    self.agent_turn = False
                    await self.respond()
                next_tick += 0.02
                await asyncio.sleep(max(0, next_tick - time.monotonic()))
        finally:
            for track in tracks:
                track.close()


def prepare(service, case):
    stamp = now()
    data = dict(case["brief"])
    data["facts"] = [
        {
            "name": name,
            "value": value,
            "provenance": "owner-approved simulated task",
            "verified_at": stamp,
            "expires_at": stamp + timedelta(minutes=15),
        }
        for name, value in data["facts"].items()
    ]
    data["disclosures"] = [f["name"] for f in data["facts"]]
    data["actions"] = [SOURCE_ACTION, RESULT_ACTION, *data["actions"]]
    data["constraints"] = [SIMULATED_CONSTRAINT, *data.get("constraints", [])]
    data.update(
        destination="+12125550100",
        deadline=stamp + timedelta(minutes=10),
        max_seconds=case.get("max_seconds", 180),
        budget_usd="3",
        synthetic=True,
    )
    data["prepared_sources"] = [
        {
            "provider": "owned_fixture",
            "source_id": s["source_id"],
            "text": s["text"],
            "truncated": s.get("truncated", True),
            "verified_at": stamp,
            "expires_at": stamp + timedelta(minutes=15),
        }
        for s in case.get("prepared_sources", [])
    ]
    cid = service.prepare("owner", PrepareCall.model_validate(data))["call_id"]
    call = service.call("owner", cid)
    call.update(state="active", provider_sid="owned-adaptive-audio-fixture")
    service.save(call)
    return call


def configuration_hash():
    files = [
        "dots_phone/call_task.py",
        "dots_phone/account_task.py",
        "dots_phone/carrier_stream.py",
        "dots_phone/task_voice.py",
        "dots_phone/models.py",
        "dots_phone/config.py",
        "dots_phone/service.py",
        "dots_phone/source_search.py",
        "dots_phone/store.py",
        "dots_phone/voice_prompt.py",
        "dots_phone/pipecat_voice.py",
        "dots_phone/pipecat_realtime.py",
        "dots_phone/local_trip_mail_worker.py",
        "dots_phone/local_context_worker.py",
        "dots_phone/prepared_evidence.py",
        "experiments/pipecat/connected_trip_mail_worker.mjs",
        "experiments/pipecat/information_source_worker.mjs",
        "experiments/pipecat/adaptive_receptionist.py",
    ]
    return hashlib.sha256(
        b"".join(name.encode() + Path(name).read_bytes() for name in files)
    ).hexdigest()


async def run(args, case, frozen_hash):
    output = args.output.resolve() / ("run-" + case["id"] + "-" + str(time.time_ns()))
    output.mkdir(parents=True, mode=0o700)
    bridge = output / "bridge"
    bridge.mkdir(mode=0o700)
    key = json.loads(read_private(args.runtime))["OPENAI_API_KEY"]
    policy = json.loads(read_private(args.policy))
    settings = Settings(
        profile="evaluation",
        database=output / "state.db",
        allowed_numbers=["+12125550100"],
        decision_seconds=45,
        trace_realtime_stages=True,
        retain_transcript=True,
        pipecat_engine="realtime",
    )
    provider = AudioProvider()
    store = Store(settings.database, settings.encryption_key.get_secret_value())
    service = PhoneService(settings, store, provider)
    service.ready = True
    call = prepare(service, case)
    private_json(
        bridge / "registry.json",
        {
            "schema": 6,
            "cases": [
                {
                    "call_id": call["call_id"],
                    "revision": 1,
                    "brief_hash": call["brief_hash"],
                    "provider": "call_sources",
                    "max_requests": 10,
                    "mail_scope": {"yahoo": policy["mail_scope"]["yahoo"]}
                    if case.get("connected_yahoo")
                    else {},
                    "fixture_sources": case.get("sources", []),
                }
            ],
        },
    )
    service.local_context_worker = LocalTripMailWorker(service, bridge)
    evidence = Evidence()
    mark = evidence.mark

    def retain_event(kind, **fields):
        event = mark(kind, **fields)
        if kind not in {"input_audio", "output_audio"}:
            with (output / "diagnostic-events.jsonl").open("a") as stream:
                stream.write(json.dumps(event) + "\n")
        return event

    evidence.mark = retain_event
    evidence.capture = AudioCapture(output / "received-audio")
    conversation = Conversation(output, evidence)
    conversation.attempt_budget_usd = args.attempt_budget_usd
    conversation.business = case["business"]
    conversation.business_state = dict(case["business"]["initial_state"])
    private_json(
        output / "business-state.json", {"state": conversation.business_state, "history": []}
    )
    llm, context, prompt = create_task_voice(
        service, call, llm_class=CapturedRealtime, api_key=key, evidence=evidence
    )
    provider.llm = llm
    conversation.llm, conversation.service = llm, service
    llm.clear_playback = conversation.clear_playback
    private_json(output / "case.json", case)
    private_json(
        output / "caller-configuration.json",
        {
            "frozen_code_sha256": frozen_hash,
            "model": settings.pipecat_realtime_model,
            "voice": "marin",
            "max_output_tokens": llm._settings.session_properties.max_output_tokens,
            "silence_duration_ms": 1000,
            "reasoning": llm._settings.session_properties.reasoning.effort,
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "factory": "dots_phone.task_voice.create_task_voice",
            "case_id": case["id"],
        },
    )
    user, assistant = LLMContextAggregatorPair(context)
    worker = PipelineWorker(
        Pipeline([user, llm, PlayedAudio(conversation), assistant]),
        params=PipelineParams(audio_in_sample_rate=24000, audio_out_sample_rate=24000),
        idle_timeout_secs=call["brief"]["max_seconds"],
    )
    conversation.worker = worker
    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    node = pipeline = receiver = link = None
    failure = None
    close_code = None
    try:
        node = await asyncio.create_subprocess_exec(
            "node",
            "experiments/pipecat/information_source_worker.mjs",
            str(bridge),
            policy["gmail_account"],
            str(case.get("read_delay_ms", 0)),
            str(call["brief"]["max_seconds"]),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        ready = json.loads(await asyncio.wait_for(node.stdout.readline(), 45))
        if not ready.get("ready"):
            raise RuntimeError("source_worker_unavailable")
        async with websockets.connect(
            "wss://api.openai.com/v1/realtime?model=" + MODEL,
            additional_headers={"Authorization": "Bearer " + key},
        ) as socket:
            conversation.socket = socket
            conversation.clerk_instructions = (
                RECEPTIONIST
                + "\nPrivate business data, never shown to the caller:\n"
                + json.dumps(case["business"])
            )
            await conversation.clerk_send(
                {
                    "type": "session.update",
                    "session": {
                        "type": "realtime",
                        "model": MODEL,
                        "instructions": conversation.clerk_instructions,
                        "output_modalities": ["audio"],
                        "reasoning": {"effort": "minimal"},
                        "max_output_tokens": 1024,
                        "tools": [
                            {
                                "type": "function",
                                "name": "wait_for_answer",
                                "description": "Wait silently while the caller is retrieving a still-unanswered fact. Never wait after a greeting or a question directed to you.",
                                "parameters": {"type": "object", "properties": {}},
                            },
                            {
                                "type": "function",
                                "name": "update_business_record",
                                "description": "Update this simulated business record's mutable fields. Convey the actual accepted or refused result; never claim an unperformed change.",
                                "parameters": {
                                    "type": "object",
                                    "properties": {"updates": {"type": "object"}},
                                    "required": ["updates"],
                                },
                            },
                        ],
                        "audio": {
                            "input": {
                                "format": {"type": "audio/pcm", "rate": 24000},
                                "turn_detection": {
                                    "type": "server_vad",
                                    "create_response": False,
                                    "interrupt_response": False,
                                    "silence_duration_ms": 700,
                                },
                            },
                            "output": {
                                "voice": "cedar",
                                "format": {"type": "audio/pcm", "rate": 24000},
                            },
                        },
                    },
                }
            )
            receiver = asyncio.create_task(conversation.receive_clerk())
            await asyncio.wait_for(conversation.clerk_ready.wait(), 15)
            pipeline = asyncio.create_task(runner.run())
            await asyncio.wait_for(llm.ready.wait(), 15)
            link = asyncio.create_task(conversation.link())
            await conversation.respond(
                "Say ONLY one brief business greeting and ask how you can help. Do not verify details or ask the unexpected question yet.",
                speech_only=True,
            )
            stop = asyncio.create_task(provider.stopped.wait())
            try:
                async with asyncio.timeout(call["brief"]["max_seconds"]):
                    done, _ = await asyncio.wait(
                        [stop, receiver, link, pipeline], return_when=asyncio.FIRST_COMPLETED
                    )
                    for completed in done:
                        completed.result()
                    if not stop.done():
                        raise RuntimeError("conversation_ended_without_recipient_stop")
            finally:
                stop.cancel()
                await asyncio.gather(stop, return_exceptions=True)
            # Drain caller usage and close the actual production voice session.
            await worker.queue_frames([EndFrame()])
            await asyncio.wait_for(pipeline, 15)
            link.cancel()
            await asyncio.gather(link, return_exceptions=True)
        close_code = socket.close_code
    except (Exception, asyncio.CancelledError) as exc:
        failure = type(exc).__name__ + ": " + str(exc)[:180]
    finally:
        if pipeline and not pipeline.done():
            await worker.queue_frames([EndFrame()])
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(pipeline, 15)
        for task in (receiver, link):
            if task and not task.done():
                task.cancel()
            if task:
                await asyncio.gather(task, return_exceptions=True)
        if conversation.socket:
            close_code = conversation.socket.close_code
        (bridge / "finished.signal").touch(mode=0o600)
        if node:
            try:
                await asyncio.wait_for(node.wait(), 5)
            except TimeoutError:
                node.terminate()
                await node.wait()
            (output / "source-worker-errors.log").write_bytes(await node.stderr.read())
        evidence.capture.close()
        current = service.call("owner", call["call_id"])
        if current["state"] not in {"completed", "failed", "cancelled"}:
            await service.end(
                "owner", call["call_id"], "fixture_deadline" if failure else "fixture_closed"
            )
            current = service.call("owner", call["call_id"])
        actual_result = service.result("owner", call["call_id"])
        result = {
            "failure": failure,
            "phone_calls": 0,
            "additional_text_api_calls": 0,
            "caller_usage_confirmed": current["usage"].get("realtime_usage_confirmed", False),
            "caller_closed": llm.closed.is_set(),
            "receptionist_usage_confirmed": bool(conversation.created)
            and conversation.created == set(conversation.usage)
            and all(conversation.usage.values()),
            "receptionist_close_code": close_code,
            "receptionist_usage": conversation.usage,
            "voice_cost_estimate_usd": round(
                voice_cost(conversation.usage)
                + voice_cost(current["usage"].get("realtime_response_usage", {})),
                6,
            ),
            "attempt_reservation_usd": args.attempt_budget_usd,
            "billing_confirmed": False,
            "recipient_stop_observed": provider.stopped.is_set(),
            "interruption_offered": conversation.interruption_offered,
            "overlap_frames": conversation.overlap_frames,
            "call_result": actual_result,
            "business_state": conversation.business_state,
            "business_history": conversation.business_history,
            "frozen_configuration_unchanged": configuration_hash() == frozen_hash,
            "call": current,
            "timeline": evidence.timeline,
            "errors": evidence.errors,
            "case_id": case["id"],
            "read_delay_ms": case.get("read_delay_ms", 0),
            "verdict": "requires_independent_acoustic_review",
        }
        private_json(output / "result.json", result)
        for path in output.rglob("*"):
            path.chmod(0o700 if path.is_dir() else 0o600)
        store.close()
    print(
        json.dumps(
            {
                "output": str(output),
                "failure": failure,
                "caller_closed": result["caller_closed"],
                "caller_usage_confirmed": result["caller_usage_confirmed"],
                "receptionist_usage_confirmed": result["receptionist_usage_confirmed"],
                "outcome": actual_result["outcome"],
                "interruption_offered": conversation.interruption_offered,
            }
        ),
        flush=True,
    )
    return {
        "case_id": case["id"],
        "output": str(output),
        "failure": failure,
        "outcome": actual_result["outcome"],
        "provider_failure": current["usage"].get("realtime_response_failure"),
    }


async def batch(args):
    cases = json.loads(read_private(args.cases))["cases"]
    if args.case:
        cases = [c for c in cases if c["id"] == args.case]
    if not cases or any(not c["id"].replace("_", "").isalnum() for c in cases):
        raise ValueError("invalid case selection")
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.attempt_budget_usd is not None and (args.output / "reservations.json").exists():
        raise ValueError("budgeted batch already reserved; use a separately accounted batch")
    frozen = configuration_hash()
    results = []
    reserved = 0.0
    for case in cases:
        if configuration_hash() != frozen:
            raise RuntimeError("caller configuration changed during the batch")
        if args.attempt_budget_usd is not None:
            if reserved + args.attempt_budget_usd > args.batch_budget_usd + 1e-9:
                break
            reserved += args.attempt_budget_usd
            private_json(
                args.output / "reservations.json",
                {
                    "reserved_usd": reserved,
                    "limit_usd": args.batch_budget_usd,
                    "next_case": case["id"],
                    "billing_confirmed": False,
                },
            )
        results.append(await run(args, case, frozen))
        latest = results[-1]
        if latest["provider_failure"] or any(
            marker in (latest["failure"] or "")
            for marker in ("insufficient_quota", "credit_balance_exhausted")
        ):
            break  # Preserve the attempt; do not repeatedly probe exhausted funding.
        if (args.output / "stop-after-current.signal").exists():
            break
    private_json(
        args.output / "batch.json",
        {
            "frozen_configuration": frozen,
            "interrupted": len(results) != len(cases),
            "runs": results,
            "verdict": "requires_independent_business_state_and_acoustic_review",
        },
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--case")
    parser.add_argument("--attempt-budget-usd", type=float)
    parser.add_argument("--batch-budget-usd", type=float)
    args = parser.parse_args()
    if (args.attempt_budget_usd is None) != (args.batch_budget_usd is None) or (
        args.attempt_budget_usd is not None
        and not 0.25 <= args.attempt_budget_usd <= args.batch_budget_usd <= 3
    ):
        parser.error("supply bounded attempt and batch budgets together")
    os.umask(0o077)
    logger.remove()
    logger.add(lambda _: None, level="ERROR")
    asyncio.run(batch(args))
