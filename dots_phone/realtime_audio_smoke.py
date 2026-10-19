"""One bounded, fictional audio exchange through the production Realtime runtime.

The IVR uses local macOS speech recordings. There is no carrier implementation,
no live connected data, no automatic retry and no second paid voice participant.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import re
import subprocess
import time
import wave
from array import array
from pathlib import Path
from types import SimpleNamespace

from dots_phone.call_reasoner import (
    AutonomousAccountWorker,
    CallReasoner,
    requires_reasoner_consultation,
)
from dots_phone.config import Settings
from dots_phone.models import Brief
from dots_phone.service import PhoneService
from dots_phone.store import Store
from dots_phone.text_eval import fixture_call
from dots_phone.text_eval_agents import CodexCallEvalWorker

LINES = [
    "Welcome to the automated rewards line. For your available points balance, press 2. For a representative, press 0.",
    "Please say your eight digit member number.",
    "I heard zero zero four two zero four two zero. Is that correct? Please say yes or no.",
    "Your available rewards balance is twelve thousand three hundred forty points. This is the available balance, not pending or lifetime points. That completes this inquiry.",
]
CLARIFICATION_LINE = (
    "Your available rewards balance is twelve thousand three hundred forty points. "
    "This is the current available balance."
)
INCORRECT_BALANCE_TRANSCRIPT = LINES[3].replace("hundred forty", "hundred")
MAX_SECONDS = 90
MAX_RESPONSES = 10
BUDGET_USD = 1.0
HEADROOM_USD = 0.25


def balance_clarification_requested(text):
    """Recognize a narrow fictional recipient response, never general routing."""
    text = " ".join(text.casefold().split())
    subject = re.search(r"\b(?:balance|points|amount|total|figure)\b", text)
    request = re.search(
        r"\b(?:(?:can|could|would|will) you (?:please )?|please )"
        r"(?:repeat|confirm|clarify|say|read|state|tell|check)\b",
        text,
    )
    readback = re.search(r"\b(?:was that|did you say|is that|is the)\b", text)
    amount = re.search(r"\b(?:\d[\d,]*|twelve thousand)\b", text)
    return bool(subject and (request or (readback and amount)))


def fixture_result_verified(conclusion):
    """Conservatively verify this fixture's subject, amount, units and polarity.

    This is a fixture rubric, not a general semantic verifier. A correct number
    in a negation, unrelated points category or quotation cannot establish the
    reported available balance. Unrecognized paraphrases fail for human review.
    """

    def matches(text):
        normalized = re.sub(r"(?<=\d),(?=\d)", "", text.casefold())
        normalized = re.sub(
            r"\btwelve thousand three hundred (?:and )?forty\b", "12340", normalized
        )
        # The fixture itself explicitly distinguishes available from these two
        # categories. Do not mistake that exclusion for a negated balance.
        normalized = re.sub(
            r"\bnot (?:pending or lifetime|lifetime or pending)(?: points)?\b", "", normalized
        )
        if re.search(
            r"\b(?:not|no|never|pending|lifetime|previous|previously|yesterday|"
            r"was|were|will|would|could|might|may|maybe|perhaps|approximately|"
            r"about|around|estimated|over|under|more|less|at least|at most|"
            r"incorrect|wrong|false|untrue|unconfirmed|unverified|uncertain)\b",
            normalized,
        ):
            return False
        # Reject a correct value accompanied by a second, unsupported amount.
        numbers = re.findall(r"\b\d+(?:\.\d+)?\b", normalized)
        if not numbers or any(number != "12340" for number in numbers):
            return False
        if re.search(
            r"\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|"
            r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|"
            r"eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|"
            r"eighty|ninety|hundred|thousand|million)\b",
            normalized,
        ):
            return False
        return bool(
            re.search(
                r"\bavailable (?:rewards? )?balance\s*(?:is\s*|of\s*|:)?\s*12340 points\b",
                normalized,
            )
            or re.search(
                r"\bavailable (?:rewards? )?points balance\s*(?:is\s*|of\s*|:)?\s*12340\b",
                normalized,
            )
        )

    if conclusion.get("outcome") != "completed":
        return False
    found = False
    for fact in conclusion.get("confirmed_facts", []):
        text, quote = fact.get("text", ""), fact.get("quote", "")
        if not re.search(r"\b(?:balance|points)\b", text + " " + quote, re.I):
            continue
        if not matches(text) or not matches(quote):
            return False
        found = True
    return found


def acoustic_turn_latencies(ivr_pcm, assistant_pcm, turns, *, sample_rate=24000):
    """Measure played speech from 20 ms PCM frames, excluding trailing silence.

    RMS >= 100 is an explicit, reproducible acoustic threshold. These are speech
    onset measurements, not proof the first audio is a useful/correct answer.
    Input/output transcripts and the saved recording must establish that.
    """
    frame_samples = sample_rate // 50
    ivr_samples, assistant_samples = array("h", ivr_pcm), array("h", assistant_pcm)

    def voiced_edges(samples, start, end):
        voiced = []
        for offset in range(start, min(end, len(samples)), frame_samples):
            frame = samples[offset : min(offset + frame_samples, end)]
            if frame and sum(value * value for value in frame) / len(frame) >= 100**2:
                voiced.append((offset, offset + len(frame)))
        return (voiced[0][0], voiced[-1][1]) if voiced else (None, None)

    rows = []
    for index, turn in enumerate(turns):
        start = turn["audio_start_sample"]
        end = turns[index + 1]["audio_start_sample"] if index + 1 < len(turns) else len(ivr_samples)
        _, input_end = voiced_edges(ivr_samples, start, end)
        output_start, _ = voiced_edges(assistant_samples, start, end)
        rows.append(
            {
                "turn": turn["turn"],
                "recipient_voiced_end_ms": round(input_end * 1000 / sample_rate, 1)
                if input_end is not None
                else None,
                "assistant_voiced_start_ms": round(output_start * 1000 / sample_rate, 1)
                if output_start is not None
                else None,
                "first_audible_reply_gap_ms": round(
                    (output_start - input_end) * 1000 / sample_rate, 1
                )
                if input_end is not None and output_start is not None
                else None,
            }
        )
    return {
        "method": "Played mono PCM, 20 ms frames, RMS >= 100; excludes recipient trailing silence.",
        "resolution_ms": 20,
        "useful_answer_verified_from_audio": False,
        "turns": rows,
    }


def response_cost(usage):
    """Conservative estimate from provider tokens; unknown usage never costs zero."""
    if not usage or "input_tokens" not in usage or "output_tokens" not in usage:
        raise ValueError("response usage unavailable")
    inputs = usage.get("input_token_details") or {}
    outputs = usage.get("output_token_details") or {}
    cached = inputs.get("cached_tokens_details") or {}
    # Unknown token modalities are charged at the more expensive audio rate.
    text = inputs.get("text_tokens", 0)
    audio = inputs.get("audio_tokens", 0)
    unknown = max(0, usage["input_tokens"] - text - audio)
    text_cached = min(text, cached.get("text_tokens", 0))
    audio_cached = min(audio, cached.get("audio_tokens", 0))
    output_text = outputs.get("text_tokens", 0)
    output_audio = outputs.get("audio_tokens", 0)
    output_unknown = max(0, usage["output_tokens"] - output_text - output_audio)
    return (
        (text - text_cached) * 4
        + (audio - audio_cached + unknown) * 32
        + (text_cached + audio_cached) * 0.4
        + output_text * 24
        + (output_audio + output_unknown) * 64
    ) / 1_000_000


def check_budget(usages, created_count):
    spent = sum(response_cost(row) for row in usages.values())
    if spent >= BUDGET_USD - HEADROOM_USD or created_count >= MAX_RESPONSES:
        raise RuntimeError("audio smoke budget or response limit reached")
    return spent


def save(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n")
    path.chmod(0o600)


def prepare(
    directory,
    *,
    response_policy="direct_with_codex",
    voice_effort="medium",
    allow_balance_clarification=False,
    inject_balance_transcript_mismatch=False,
):
    if inject_balance_transcript_mismatch and not allow_balance_clarification:
        raise ValueError("transcription fault requires the explicit clarification fixture")
    if (directory / "attempt.json").exists():
        raise FileExistsError("refusing to change fixtures for an existing audio attempt")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    lines = [*LINES, CLARIFICATION_LINE] if allow_balance_clarification else LINES
    clips = []
    for index, line in enumerate(lines):
        path = directory / f"ivr-{index}.wav"
        if not path.exists():
            subprocess.run(
                ["say", "--file-format=WAVE", "--data-format=LEI16@24000", "-o", str(path), line],
                check=True,
                capture_output=True,
            )
        with wave.open(str(path), "rb") as stream:
            if (stream.getnchannels(), stream.getsampwidth(), stream.getframerate()) != (
                1,
                2,
                24000,
            ):
                raise ValueError("fixture must be mono 24 kHz PCM")
            pcm = stream.readframes(stream.getnframes())
        clips.append(pcm)
        path.chmod(0o600)
    scope = {
        "model": "gpt-realtime-2.1",
        "reasoner_model": "gpt-6-sol",
        "response_policy": response_policy,
        "voice_effort": voice_effort,
        "allow_balance_clarification": allow_balance_clarification,
        "max_recipient_clarifications": 1 if allow_balance_clarification else 0,
        "inject_balance_transcript_mismatch": inject_balance_transcript_mismatch,
        "transcript_fault_scope": (
            "Actual provider item IDs bind supervisor-only transcript replacement: "
            "initial balance 12,300; requested clarification 12,340. "
            "Raw provider transcripts and native input audio remain retained and unchanged. "
            "This mode tests report repair, not natural transcription accuracy."
            if inject_balance_transcript_mismatch
            else None
        ),
        "reasoner_transport": "signed_in_codex_fictional_tools",
        "reasoner_lifecycle": "one_prepared_production_agent_per_call",
        "runs": 1,
        "budget_usd": BUDGET_USD,
        "inflight_and_transcription_headroom_usd": HEADROOM_USD,
        "max_seconds": MAX_SECONDS,
        "max_responses": MAX_RESPONSES,
        "max_output_tokens_per_response": 1024,
        "phone_calls": 0,
        "real_account_queries": 0,
        "paid_fixture_generation": 0,
        "lines": lines,
        "clip_sha256": [hashlib.sha256(pcm).hexdigest() for pcm in clips],
        "scope": "Local simulated keypad acceptance, spoken verification, readback and final result; excludes PSTN and carrier reconnect.",
    }
    save(directory / "scope.json", scope)
    return clips


class Trace:
    def __init__(self, directory, *, inject_balance_transcript_mismatch=False):
        self.directory = directory
        self.origin = time.monotonic()
        self.events = []
        self.agent_audio = bytearray()
        self.captions = []
        self.digits = []
        self.playing = False
        self.last_played = 0.0
        self.forwarded_before_reasoning = 0
        self.abort = asyncio.Event()
        self.failure = None
        self.audio_samples = 0
        self.completed_response_usage = {}
        self.current_fixture_turn = None
        self.input_turn_by_item = {}
        self.inject_balance_transcript_mismatch = inject_balance_transcript_mismatch
        self.transcription_fault_items = {}
        self.balance_clarifications_played = 0

    def observed_input_transcription(self, event):
        """Retain raw input, optionally fault only the supervisor's observation."""
        item_id = event.get("item_id")
        turn = self.input_turn_by_item.get(item_id)
        self.mark(
            "provider_input_transcript",
            text=event.get("transcript"),
            item_id=item_id,
            fixture_turn=turn,
            usage=event.get("usage"),
            raw_event=event,
        )
        effective = event
        if self.inject_balance_transcript_mismatch:
            if turn is None and self.current_fixture_turn in {3, 4}:
                self.failure = "transcription_fault_missing_recipient_item_binding"
                self.abort.set()
            elif turn in {3, 4}:
                previous = self.transcription_fault_items.get(turn)
                if previous is not None and previous != item_id:
                    self.failure = "transcription_fault_multiple_items_in_fixture_turn"
                    self.abort.set()
                else:
                    self.transcription_fault_items[turn] = item_id
                    text = INCORRECT_BALANCE_TRANSCRIPT if turn == 3 else CLARIFICATION_LINE
                    effective = {**event, "transcript": text}
                    self.mark(
                        "transcription_fault_injected",
                        fixture_turn=turn,
                        item_id=item_id,
                        original_text=event.get("transcript"),
                        effective_text=text,
                    )
        self.mark(
            "input_transcript",
            text=effective.get("transcript"),
            item_id=item_id,
            fixture_turn=turn,
            synthetic_override=effective is not event,
            usage=event.get("usage"),
        )
        return effective

    def mark(self, kind, **fields):
        event = {"kind": kind, "at_ms": round((time.monotonic() - self.origin) * 1000), **fields}
        self.events.append(event)
        with (self.directory / "events.jsonl").open("a") as stream:
            stream.write(json.dumps(event) + "\n")
        return event


def build_voice(service, call, key, trace):
    from pipecat.frames.frames import OutputAudioRawFrame
    from pipecat.processors.frame_processor import FrameProcessor

    from dots_phone.pipecat_realtime import SupervisedRealtimeService
    from dots_phone.task_voice import create_task_voice

    class ObservedRealtime(SupervisedRealtimeService):
        async def observe(self, event):
            kind = event["type"]
            if kind in {"input_audio_buffer.speech_started", "input_audio_buffer.speech_stopped"}:
                if kind == "input_audio_buffer.speech_started" and event.get("item_id"):
                    trace.input_turn_by_item[event["item_id"]] = trace.current_fixture_turn
                trace.mark(kind, item_id=event.get("item_id"))
            elif kind == "response.created":
                trace.mark(kind, response_id=event["response"]["id"])
            elif kind == "response.done":
                response = event["response"]
                # A new recipient turn can produce a second terminal event for
                # an already completed response. Preserve that raw event, but
                # do not call it an incomplete generation when the completed
                # response and identical metered usage were already observed.
                repeated_completed_cancellation = (
                    response["status"] == "cancelled"
                    and response["id"] in trace.completed_response_usage
                    and response.get("usage") == trace.completed_response_usage[response["id"]]
                    and self.caller_speaking
                    and response["id"] not in self.active_responses
                )
                trace.mark(
                    kind,
                    response_id=response["id"],
                    status=response["status"],
                    status_details=response.get("status_details"),
                    usage=response.get("usage"),
                    repeated_completed_cancellation=repeated_completed_cancellation,
                )
                if response["status"] == "completed" and response.get("usage"):
                    trace.completed_response_usage[response["id"]] = response["usage"]
                if not response.get("usage") or (
                    response["status"] != "completed" and not repeated_completed_cancellation
                ):
                    trace.failure = "response_incomplete_or_usage_unconfirmed"
                    trace.abort.set()
            elif kind == "response.function_call_arguments.done":
                trace.mark("tool_call", name=event.get("name"), arguments=event.get("arguments"))
            elif kind == "conversation.item.input_audio_transcription.completed":
                event = trace.observed_input_transcription(event)
            elif kind in {
                "response.output_audio_transcript.done",
                "response.audio_transcript.done",
            }:
                if (
                    event.get("response_id") not in self.reasoner_blocked_responses
                    and self.speech_phases.get(event.get("item_id")) != "commentary"
                ):
                    trace.captions.append(event.get("transcript", ""))
                    trace.mark("output_transcript", text=event.get("transcript", ""))
                else:
                    trace.mark("suppressed_output_transcript", text=event.get("transcript", ""))
            elif kind == "error":
                trace.mark("provider_error", code=(event.get("error") or {}).get("code"))
                trace.failure = "provider_error"
                trace.abort.set()
            await super().observe(event)
            if kind in {"response.done", "response.created"}:
                try:
                    check_budget(self.response_usage, len(self.responses_created))
                except (RuntimeError, ValueError) as exc:
                    trace.failure = str(exc)
                    trace.abort.set()

        async def send_client_event(self, event):
            if event.type == "response.create":
                check_budget(self.response_usage, len(self.responses_created))
            await super().send_client_event(event)

        def voice_failure(self, phase, error=None):
            # One attempt only: a transport failure ends this test, without a
            # paid reconnection that could duplicate or obscure usage.
            if self.closing or self.stop_requested:
                return
            trace.failure = "voice_failure:" + phase
            trace.mark("voice_failure", phase=phase)
            trace.abort.set()

    llm, context, prompt = create_task_voice(service, call, llm_class=ObservedRealtime, api_key=key)
    from dots_phone.call_task import task_tools
    from dots_phone.keypad_tools import task_keypad_tools

    tools, keypad_prompt = task_keypad_tools(
        service.provider,
        llm,
        call,
        task_tools(llm, source_tools=False, host_sources=False, reasoner=True),
        account=True,
    )
    context.set_tools(tools)
    llm.append_system_instruction(keypad_prompt)
    prompt = llm._settings.system_instruction
    llm._settings.session_properties.tools = context.tools
    llm._sync_registered_tool_handlers(context.tools)

    class PlayedOutput(FrameProcessor):
        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            if isinstance(frame, OutputAudioRawFrame):
                if llm.reasoner_consulted_turn != llm.reasoner_turn:
                    trace.forwarded_before_reasoning += len(frame.audio)
                if not trace.agent_audio and not trace.playing:
                    trace.mark("playback_enqueued", samples=len(frame.audio) // 2)
                trace.agent_audio.extend(frame.audio)
            await self.push_frame(frame, direction)

    return llm, context, prompt, PlayedOutput()


def audio_provider(settings, service, trace):
    from dots_phone.pipecat_voice import PipecatTelephony

    class NoDialProvider(PipecatTelephony):
        def __init__(self):
            super().__init__(settings, service, client=object())
            self.stopped = asyncio.Event()
            self.llm = None

        async def dial(self, call):
            raise RuntimeError("audio smoke has no phone carrier")

        async def stop(self, call):
            trace.mark("call_stopped")
            self.stopped.set()

        async def close_voice(self, call):
            pass

        async def update(self, call, delegation, text):
            trace.mark("reasoner_result", output=json.loads(text))
            await self.llm.deliver_decision(call, delegation, text)

        async def send_phone_digits(self, call, session, digits, *, audit=None, origin=None):
            # Inherit production menu/scope/repeat checks, replacing carrier I/O
            # only. This verifies a submitted control, not real carrier DTMF.
            await session.llm.clear_playback("keypad_control")
            self.prepare_keypad_submission(call["call_id"], session, origin, audit=audit)
            trace.digits.append(digits)
            trace.mark("simulated_keypad_accepted", digits=digits)
            return {"carrier_control_accepted": True, "synthetic": True}

    return NoDialProvider()


async def run(
    directory,
    runtime_path,
    *,
    response_policy="direct_with_codex",
    voice_effort="medium",
    allow_balance_clarification=False,
    inject_balance_transcript_mismatch=False,
):
    from pipecat.frames.frames import BotStoppedSpeakingFrame, EndFrame, InputAudioRawFrame
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.worker import PipelineParams, PipelineWorker
    from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
    from pipecat.workers.runner import WorkerRunner

    clips = prepare(
        directory,
        response_policy=response_policy,
        voice_effort=voice_effort,
        allow_balance_clarification=allow_balance_clarification,
        inject_balance_transcript_mismatch=inject_balance_transcript_mismatch,
    )
    lines = [*LINES, CLARIFICATION_LINE] if allow_balance_clarification else LINES
    # Durable claim before any paid session; rerunning the same directory
    # refuses rather than silently starting a second experiment.
    with (directory / "attempt.json").open("x") as stream:
        json.dump({"started_at": time.time(), "budget_usd": BUDGET_USD}, stream)
    trace = Trace(directory, inject_balance_transcript_mismatch=inject_balance_transcript_mismatch)
    settings = Settings(
        profile="evaluation",
        database=directory / "state.db",
        allowed_numbers=["+12125550100"],
        voice_backend="pipecat",
        pipecat_engine="realtime",
        account_tasks=True,
        pipecat_account_dtmf=True,
        decision_seconds=60,
        trace_realtime_stages=True,
        retain_transcript=True,
        local_reasoner_voice_effort=voice_effort,
    )
    store = Store(settings.database, settings.encryption_key.get_secret_value())
    service = PhoneService(settings, store)
    service.ready = True
    provider = audio_provider(settings, service, trace)
    service.provider = provider
    data = fixture_call({})["brief"]
    data.update(max_seconds=MAX_SECONDS, budget_usd="1.00")
    data["actions"].remove("read_call_sources")
    call_id = service.prepare("owner", Brief.model_validate(data))["call_id"]
    call = service.call("owner", call_id)
    call.update(
        state="active",
        runtime_supervision="local_reasoner",
        provider_sid="local-audio-fixture",
        reasoner_response_policy=response_policy,
    )
    service.save(call)
    settings.dry_run = False  # Only the injected NoDialProvider can be reached.
    codex = await CodexCallEvalWorker.start(call, directory)
    trace.mark("reasoner_prepared", **codex.lifecycle_trace[-1])
    service.local_context_worker = AutonomousAccountWorker(
        service, reasoner=CallReasoner(service, client=codex)
    )
    key = json.loads(runtime_path.read_text())["OPENAI_API_KEY"]
    llm, context, prompt, output = build_voice(service, call, key, trace)
    provider.llm = llm
    provider.sessions[call["provider_sid"]] = SimpleNamespace(
        llm=llm, carrier=object(), ending=False, menu_lock=asyncio.Lock(), used_menus=set()
    )
    save(
        directory / "configuration.json",
        {
            "factory": "dots_phone.task_voice.create_task_voice",
            "max_output_tokens_per_response": llm._settings.session_properties.max_output_tokens,
            "fixture_modes": {
                "allow_balance_clarification": allow_balance_clarification,
                "inject_balance_transcript_mismatch": inject_balance_transcript_mismatch,
            },
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "source_sha256": {
                name: hashlib.sha256(Path(name).read_bytes()).hexdigest()
                for name in (
                    "dots_phone/task_voice.py",
                    "dots_phone/pipecat_realtime.py",
                    "dots_phone/call_reasoner.py",
                    "dots_phone/call_task.py",
                    "dots_phone/report_review.py",
                    "dots_phone/prepared_evidence.py",
                    "dots_phone/service.py",
                    "dots_phone/realtime_audio_smoke.py",
                    "dots_phone/voice_prompt.py",
                    "dots_phone/keypad_tools.py",
                    "experiments/pipecat/codex_call_reasoner.mjs",
                    "experiments/pipecat/subscription_app_server.mjs",
                    "dots_phone/text_eval_agents.py",
                )
            },
        },
    )
    user, assistant = LLMContextAggregatorPair(context)
    worker = PipelineWorker(
        Pipeline([user, llm, output, assistant]),
        params=PipelineParams(audio_in_sample_rate=24000, audio_out_sample_rate=24000),
        idle_timeout_secs=MAX_SECONDS,
    )
    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    pipeline = asyncio.create_task(runner.run())
    incoming = bytearray()
    tracks = [
        wave.open(str(directory / name), "wb")
        for name in ("assistant.wav", "ivr.wav", "conversation.wav")
    ]
    for stream in tracks:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(24000)

    async def link():
        next_tick = time.monotonic()
        while not llm.closing:
            received = bytes(incoming[:960])
            del incoming[: len(received)]
            received = received.ljust(960, b"\0")
            played = bytes(trace.agent_audio[:960])
            del trace.agent_audio[: len(played)]
            played = played.ljust(960, b"\0")
            if any(abs(x) > 200 for x in array("h", played)):
                if not trace.playing:
                    trace.mark("first_played_speech")
                trace.playing = True
                trace.last_played = time.monotonic()
            elif (
                trace.playing
                and not trace.agent_audio
                and not llm.active_responses
                and time.monotonic() - trace.last_played > 0.15
            ):
                trace.playing = False
                await worker.queue_frames([BotStoppedSpeakingFrame()])
            tracks[0].writeframesraw(played)
            tracks[1].writeframesraw(received)
            mixed = array(
                "h",
                (
                    max(-32768, min(32767, a + b))
                    for a, b in zip(array("h", played), array("h", received), strict=True)
                ),
            )
            tracks[2].writeframesraw(mixed.tobytes())
            trace.audio_samples += len(received) // 2
            await worker.queue_frames([InputAudioRawFrame(received, 24000, 1)])
            next_tick += 0.02
            await asyncio.sleep(max(0, next_tick - time.monotonic()))

    async def until(predicate, seconds=22):
        async with asyncio.timeout(seconds):
            while not predicate():
                if trace.abort.is_set():
                    raise RuntimeError(trace.failure or "audio smoke aborted")
                if pipeline.done():
                    pipeline.result()
                    raise RuntimeError("voice pipeline ended early")
                await asyncio.sleep(0.02)

    def idle():
        return (
            not incoming
            and not trace.agent_audio
            and not trace.playing
            and not llm.active_responses
            and not llm.decisions
            and not llm.pending_response_requests
            and not llm.source_continuations
        )

    async def speak(index):
        check_budget(llm.response_usage, len(llm.responses_created))
        trace.current_fixture_turn = index
        trace.mark(
            "ivr_turn_started",
            turn=index,
            text=lines[index],
            audio_start_sample=trace.audio_samples,
        )
        incoming.extend(clips[index])
        await until(lambda: not incoming, 18)
        trace.mark("ivr_clip_finished", turn=index)

    link_task = None
    failure = None
    try:
        async with asyncio.timeout(MAX_SECONDS):
            await asyncio.wait_for(llm.ready.wait(), 15)
            trace.mark("voice_ready")
            link_task = asyncio.create_task(link())
            await speak(0)
            await until(lambda: bool(trace.digits))
            if trace.digits != ["2"]:
                raise RuntimeError("incorrect_menu_choice")
            await until(idle)
            before = len(trace.captions)
            await speak(1)
            await until(lambda: len(trace.captions) > before)
            await until(idle)
            number_answer = " ".join(trace.captions[before:])
            normalized = re.sub(r"\D", "", number_answer)
            if "00420420" not in normalized:
                # Word-normalized check remains deterministic and independent
                # of the known briefing fact's presence in the caller prompt.
                words = {
                    w: str(i)
                    for i, w in enumerate(
                        "zero one two three four five six seven eight nine".split()
                    )
                }
                spoken = re.sub(
                    r"\b(" + "|".join(words) + r")\b", lambda m: words[m[0]], number_answer.lower()
                )
                if "00420420" not in re.sub(r"\D", "", spoken):
                    raise RuntimeError("incorrect_spoken_member_number")
            before = len(trace.captions)
            await speak(2)
            await until(lambda: len(trace.captions) > before)
            await until(idle)
            if not re.search(r"\byes\b", " ".join(trace.captions[before:]), re.I):
                raise RuntimeError("readback_not_confirmed")
            before = len(trace.captions)
            before_played = trace.last_played
            await speak(3)
            if allow_balance_clarification:
                await until(lambda: provider.stopped.is_set() or len(trace.captions) > before, 30)
                if not provider.stopped.is_set():
                    await until(lambda: provider.stopped.is_set() or idle())
                    if not provider.stopped.is_set():
                        request = " ".join(trace.captions[before:])
                        if (
                            not balance_clarification_requested(request)
                            or trace.last_played <= before_played
                        ):
                            raise RuntimeError("no_audible_balance_clarification_request")
                        trace.mark("balance_clarification_requested", text=request)
                        trace.balance_clarifications_played += 1
                        await speak(4)
            await until(provider.stopped.is_set, 30)
    except Exception as exc:
        failure = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        trace.mark("test_failed", failure=failure)
    finally:
        if link_task:
            link_task.cancel()
            await asyncio.gather(link_task, return_exceptions=True)
        if not pipeline.done():
            await worker.queue_frames([EndFrame()])
            try:
                await asyncio.wait_for(pipeline, 12)
            except TimeoutError:
                pipeline.cancel()
                await asyncio.gather(pipeline, return_exceptions=True)
        else:
            with contextlib.suppress(Exception):
                pipeline.result()
        await codex.close()
        for stream in tracks:
            stream.close()
        with wave.open(str(directory / "ivr.wav"), "rb") as stream:
            ivr_pcm = stream.readframes(stream.getnframes())
        with wave.open(str(directory / "assistant.wav"), "rb") as stream:
            assistant_pcm = stream.readframes(stream.getnframes())
        final = service.call("owner", call_id)
        confirmed = final["usage"].get("realtime_usage_confirmed", False)
        usage_cost = None
        if confirmed:
            usage_cost = sum(response_cost(row) for row in llm.response_usage.values())
        conclusion = final.get("conclusion") or {}
        verified = fixture_result_verified(conclusion)
        if inject_balance_transcript_mismatch and not failure:
            if set(trace.transcription_fault_items) != {3, 4}:
                failure = "transcription_fault_recovery_not_exercised"
            elif trace.balance_clarifications_played != 1:
                failure = "transcription_fault_requires_one_recipient_clarification"
        if not failure and not verified:
            failure = "incorrect_fixture_balance"
        passed = (
            not failure
            and confirmed
            and llm.closed.is_set()
            and verified
            and (not requires_reasoner_consultation(call) or trace.forwarded_before_reasoning == 0)
        )
        result = {
            "passed": passed,
            "response_policy": response_policy,
            "voice_effort": voice_effort,
            "allow_balance_clarification": allow_balance_clarification,
            "inject_balance_transcript_mismatch": inject_balance_transcript_mismatch,
            "transcription_fault_items": trace.transcription_fault_items,
            "balance_clarifications_played": trace.balance_clarifications_played,
            "failure": failure,
            "fixture_result_verified": verified,
            "call": final,
            "timeline": trace.events,
            "played_audio_latency": acoustic_turn_latencies(
                ivr_pcm,
                assistant_pcm,
                [event for event in trace.events if event["kind"] == "ivr_turn_started"],
            ),
            "reasoner_advice": codex.advice_trace,
            "reasoner_lifecycle": codex.lifecycle_trace,
            "voice_closed": llm.closed.is_set(),
            "voice_usage_confirmed": confirmed,
            "observed_response_token_cost_usd": round(usage_cost, 6)
            if usage_cost is not None
            else None,
            "cost_note": "Response-token estimate; input transcription separately reported where available. Excludes signed-in Codex allowance. $0.25 reserved as in-flight/transcription headroom.",
            "transcription_usage": [
                e.get("usage") for e in trace.events if e["kind"] == "input_transcript"
            ],
            "responses": len(llm.responses_created),
            "forwarded_audio_bytes_before_reasoning": trace.forwarded_before_reasoning,
            "phone_calls": 0,
            "real_account_queries": len(codex.tool_calls),
            "simulated_keypad_digits": trace.digits,
        }
        save(directory / "result.json", result)
        store.close()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runtime-file", type=Path)
    parser.add_argument("--live", action="store_true")
    parser.add_argument(
        "--response-policy",
        choices=["direct_with_codex", "always_consult"],
        default="direct_with_codex",
    )
    parser.add_argument(
        "--voice-effort", choices=["minimal", "low", "medium", "high"], default="medium"
    )
    parser.add_argument("--allow-balance-clarification", action="store_true")
    parser.add_argument("--inject-balance-transcript-mismatch", action="store_true")
    args = parser.parse_args()
    if args.inject_balance_transcript_mismatch and not args.allow_balance_clarification:
        parser.error("--inject-balance-transcript-mismatch requires --allow-balance-clarification")
    os.umask(0o077)
    if not args.live:
        clips = prepare(
            args.output,
            response_policy=args.response_policy,
            voice_effort=args.voice_effort,
            allow_balance_clarification=args.allow_balance_clarification,
            inject_balance_transcript_mismatch=args.inject_balance_transcript_mismatch,
        )
        print(
            json.dumps(
                {
                    "prepared_clips": len(clips),
                    "paid_requests": 0,
                    "inject_balance_transcript_mismatch": args.inject_balance_transcript_mismatch,
                }
            )
        )
        return
    if args.runtime_file is None:
        parser.error("live mode requires --runtime-file")
    from loguru import logger

    logger.remove()
    result = asyncio.run(
        run(
            args.output.resolve(),
            args.runtime_file,
            response_policy=args.response_policy,
            voice_effort=args.voice_effort,
            allow_balance_clarification=args.allow_balance_clarification,
            inject_balance_transcript_mismatch=args.inject_balance_transcript_mismatch,
        )
    )
    print(
        json.dumps(
            {
                key: result[key]
                for key in (
                    "passed",
                    "failure",
                    "observed_response_token_cost_usd",
                    "responses",
                    "voice_closed",
                    "voice_usage_confirmed",
                    "inject_balance_transcript_mismatch",
                    "balance_clarifications_played",
                )
            }
        ),
        flush=True,
    )
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
