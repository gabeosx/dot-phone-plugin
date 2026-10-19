"""Offline actual-SDK pipeline checks; native classification is NOT exercised.

Speech fixtures are prerecorded local synthetic voices. Provider start/stop
events are explicitly scripted, including for noise/acknowledgement labels.
The labels describe hypothetical causes, never a detector classification.
"""

import asyncio
import audioop
import base64
import hashlib
import json
import math
import wave
from array import array
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("soxr")

import soxr

from tests.conftest import brief, service, settings  # noqa: F401
from tests.test_realtime_interruption_ordering import (  # noqa: F401
    assert_waveform,
    no_external_connections,
)
from tests.test_realtime_interruption_ordering import (
    audio_pipeline as pipeline_fixture,
)
from tests.test_realtime_playout import MemoryRealtime, eventually

FIXTURES = Path(__file__).with_name("fixtures")
audio_pipeline = pipeline_fixture


def load_speech(name):
    from tests.audio_assets import audio_asset

    path = audio_asset(name)
    manifest = json.loads((FIXTURES / "manifest.json").read_text())
    assert manifest["synthetic"] is True
    assert hashlib.sha256(path.read_bytes()).hexdigest() == manifest["clips"][name]["sha256"]
    with wave.open(str(path)) as source:
        assert (source.getnchannels(), source.getsampwidth(), source.getframerate()) == (1, 2, 8000)
        return source.readframes(source.getnframes())


def queue_speech(model, monkeypatch):
    """Keep the existing actual-wire response helper, substituting speech PCM."""
    # Resolve optional external media before entering the streaming context:
    # a missing fixture must skip, not run stream-exit assertions on zero audio.
    clips = {
        name: load_speech(name) for name in ("synthetic-question.wav", "synthetic-recovery.wav")
    }
    prepared = {}
    offsets = {}
    original_emit = model.emit

    def emit(event):
        name = event.get("response_id")
        if event["type"] == "response.output_audio.delta" and name in prepared:
            size = len(base64.b64decode(event["delta"]))
            offset = offsets.get(name, 0)
            payload = prepared[name][offset : offset + size]
            assert len(payload) == size
            offsets[name] = offset + size
            MemoryRealtime.emit(model, {**event, "delta": base64.b64encode(payload).decode()})
        else:
            original_emit(event)

    monkeypatch.setattr(model, "emit", emit)

    def response(name, filename):
        pcm = clips[filename]
        milliseconds = len(pcm) // 16
        assert len(pcm) == milliseconds * 16
        converted, _ = audioop.ratecv(pcm, 2, 1, 8000, 24000, None)
        # ratecv omits two final interpolated samples. Zero padding here is
        # four bytes, not missing speech or an arbitrary duration allowance.
        prepared[name] = converted.ljust(milliseconds * 48, b"\x00")
        model.response(name, milliseconds=milliseconds)
        assert offsets[name] == len(prepared[name])
        return pcm

    return response


def assert_complete_speech(payload, original):
    """Exact duration plus waveform identity catches clipping, filler and reuse."""
    assert len(payload) * 2 == len(original)
    actual = np.frombuffer(audioop.ulaw2lin(payload, 2), dtype="<i2").astype(float)
    expected = np.frombuffer(original, dtype="<i2").astype(float)
    assert audioop.rms(audioop.ulaw2lin(payload, 2), 2) > 500
    # Codec/resampler filtering can move the waveform a few samples. Compare
    # every voiced 200ms window, including the final word, at a bounded lag.
    for start in range(0, len(expected), 1600):
        end = min(start + 1600, len(expected))
        if np.sqrt(np.mean(expected[start:end] ** 2)) < 150:
            continue
        correlations = []
        for lag in range(-12, 13):
            a, b = max(start, -lag), min(end, len(actual) - lag)
            correlations.append(np.corrcoef(expected[a:b], actual[a + lag : b + lag])[0, 1])
        assert max(correlations) > 0.95, (start / 8000, max(correlations))


class RecipientAudio:
    """Feed deterministic background plus attenuated outgoing echo continuously."""

    def __init__(self, pipeline):
        self.pipeline = pipeline
        self.count = 0
        self.stopping = False
        self.baseline = pipeline.llm.media_progress["input_frames_dispatched"]
        assert self.baseline == 1  # Shared fixture sends one initial silence frame.
        self.source_frames = [b"\x00" * 320]
        self.task = None

    def appends(self):
        return [e for e in self.pipeline.model.sent if e["type"] == "input_audio_buffer.append"]

    def feed(self):
        media = [e for e in self.pipeline.carrier.sent if e["event"] == "media"]
        echo = (
            array("h", audioop.ulaw2lin(base64.b64decode(media[-1]["media"]["payload"]), 2))
            if media
            else array("h", [0] * 160)
        )
        offset = self.count * 160
        samples = array(
            "h",
            (
                int(
                    450 * math.sin(2 * math.pi * 137 * (offset + i) / 8000)
                    + 250 * math.sin(2 * math.pi * 1433 * (offset + i) / 8000)
                    + echo[i % len(echo)] * 0.12
                )
                for i in range(160)
            ),
        )
        payload = audioop.lin2ulaw(samples.tobytes(), 2)
        self.source_frames.append(audioop.ulaw2lin(payload, 2))
        self.pipeline.carrier.feed(
            {
                "event": "media",
                "media": {"payload": base64.b64encode(payload).decode()},
            }
        )
        self.count += 1

    async def run(self):
        while not self.stopping:
            self.feed()
            await asyncio.sleep(0.02)

    async def __aenter__(self):
        self.task = asyncio.create_task(self.run())
        return self

    async def __aexit__(self, *args):
        self.stopping = True
        await self.task
        await eventually(
            lambda: (
                self.pipeline.llm.media_progress["input_frames_dispatched"]
                == self.baseline + self.count
            )
        )
        sent = self.appends()
        assert self.count > 0 and sent
        actual = b"".join(base64.b64decode(e["audio"]) for e in sent)
        reference = soxr.ResampleStream(8000, 24000, 1, dtype="int16", quality="VHQ")
        expected = b"".join(
            reference.resample_chunk(np.frombuffer(frame, dtype="<i2")).tobytes()
            for frame in self.source_frames
        )
        # Compare all input reaching the provider, not just counters or energy.
        # Match the streaming resampler's unflushed filter tail exactly rather
        # than inventing duration for samples still buffered inside its filter.
        assert len(actual) == len(expected)
        assert self.pipeline.llm.media_progress["input_ms_dispatched"] == (self.count + 1) * 20
        np.testing.assert_allclose(
            np.frombuffer(actual, dtype="<i2").astype(int),
            np.frombuffer(expected, dtype="<i2").astype(int),
            rtol=0,
            atol=2,
        )
        assert audioop.rms(actual, 2) > 100


def scripted_native_event(model, *, started, turn, audio_ms):
    model.emit(
        {
            "type": "input_audio_buffer.speech_started"
            if started
            else "input_audio_buffer.speech_stopped",
            "item_id": f"scripted_recipient_{turn}",
            "audio_start_ms" if started else "audio_end_ms": audio_ms,
        }
    )


async def test_complete_synthetic_question_with_background_and_echo_without_native_start(
    audio_pipeline,
    monkeypatch,  # noqa: F811 -- imported pytest fixture
):
    pipeline = audio_pipeline
    updates = [e for e in pipeline.model.sent if e["type"] == "session.update"]
    detection = updates[-1]["session"]["audio"]["input"]["turn_detection"]
    assert detection["type"] == "server_vad"
    assert detection["threshold"] == 0.5
    assert detection["silence_duration_ms"] == 300
    assert detection.get("interrupt_response", True) is True
    speak = queue_speech(pipeline.model, monkeypatch)
    async with RecipientAudio(pipeline):
        original = speak("complete_question", "synthetic-question.wav")
        await eventually(lambda: pipeline.carrier.audio_bytes >= len(original) // 2, timeout=5)
        await eventually(lambda: pipeline.llm._current_audio_response is None)
    assert pipeline.carrier.clears == 0
    assert_complete_speech(pipeline.carrier.audio_after(0), original)


async def test_native_start_during_final_question_clears_old_tail_and_recovers(
    audio_pipeline,
    monkeypatch,  # noqa: F811 -- imported pytest fixture
):
    pipeline = audio_pipeline
    model, carrier, llm = pipeline.model, pipeline.carrier, pipeline.llm
    speak = queue_speech(model, monkeypatch)
    truncate_entered = asyncio.Event()
    truncate_release = asyncio.Event()
    truncations = []
    original_send = model.send

    async def held_truncate(message):
        event = json.loads(message)
        if event["type"] == "conversation.item.truncate":
            assert carrier.clears == 1
            truncations.append(event)
            truncate_entered.set()
            await asyncio.wait_for(truncate_release.wait(), 2)
        await original_send(message)

    monkeypatch.setattr(model, "send", held_truncate)
    async with RecipientAudio(pipeline) as incoming:
        question = speak("question_cut_by_native_event", "synthetic-question.wav")
        # Prerecorded sentence: "Hi, I am an AI assistant. What time is dinner?"
        # Interrupt in the final question, leaving an audible old tail queued.
        await eventually(lambda: carrier.audio_bytes >= 2640 * 8, timeout=4)
        scripted_native_event(model, started=True, turn=1, audio_ms=1000)
        scripted_native_event(model, started=True, turn=1, audio_ms=1000)
        await asyncio.wait_for(truncate_entered.wait(), 2)
        cleared = carrier.clear_events[0]
        assert 2640 * 8 <= cleared.audio_bytes < len(question) // 2
        before = llm.media_progress["input_frames_dispatched"]
        # While provider truncation is held, recipient input still traverses
        # the actual input dispatcher; no old output survives the clear.
        await eventually(lambda: llm.media_progress["input_frames_dispatched"] >= before + 4)
        assert carrier.audio_bytes == cleared.audio_bytes
        assert incoming.count > 4
        scripted_native_event(model, started=False, turn=1, audio_ms=2500)
        recovery = speak("following_distinct_response", "synthetic-recovery.wav")
        truncate_release.set()
        await eventually(
            lambda: carrier.audio_bytes >= cleared.audio_bytes + len(recovery) // 2, timeout=5
        )
        await eventually(lambda: llm._current_audio_response is None)
    assert carrier.clears == 1
    assert len(truncations) == 1
    assert truncations[0]["item_id"] == "question_cut_by_native_event_item"
    assert_complete_speech(carrier.audio_after(cleared.event_index), recovery)


@pytest.mark.parametrize(
    "hypothetical_cause,voiced_ms",
    [
        ("brief_noise", 100),
        ("short_acknowledgement", 300),
        ("deliberate_barge_in", 1200),
    ],
)
async def test_scripted_native_start_always_interrupts_regardless_of_cause_label(
    audio_pipeline,
    hypothetical_cause,
    voiced_ms,  # noqa: F811 -- imported pytest fixture
):
    """Event-replay boundary only: no claim of noise/ack semantic recognition."""
    pipeline = audio_pipeline
    model, carrier, llm = pipeline.model, pipeline.carrier, pipeline.llm
    async with RecipientAudio(pipeline):
        model.response("old_" + hypothetical_cause, milliseconds=2000, frequency=400)
        await eventually(lambda: carrier.audio_bytes >= 640)
        scripted_native_event(model, started=True, turn=1, audio_ms=1000)
        await eventually(lambda: carrier.clears == 1)
        cleared = carrier.clear_events[0]
        assert cleared.audio_bytes < 16000
        # Virtual provider timestamps include its 300ms end-of-turn silence;
        # replay is compressed, not a simulation of provider classification.
        scripted_native_event(model, started=False, turn=1, audio_ms=1000 + voiced_ms + 300)
        model.response("recovery_" + hypothetical_cause, milliseconds=800, frequency=900)
        await eventually(lambda: carrier.audio_bytes >= cleared.audio_bytes + 6400)
        await eventually(lambda: llm._current_audio_response is None)
    assert carrier.clears == 1
    assert_waveform(
        carrier.audio_after(cleared.event_index),
        milliseconds=800,
        frequency=900,
        absent_frequency=400,
    )
