import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("pipecat")

from pipecat.audio.vad.vad_analyzer import VADState  # noqa: E402
from pipecat.frames.frames import (  # noqa: E402
    InputAudioRawFrame,
    InterruptionFrame,
    LLMTextFrame,
    OutputAudioRawFrame,
    ProposedUserStartedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection  # noqa: E402

from dots_phone.pipecat_transport import (  # noqa: E402
    LiveCallerSpeech,
    LiveCarrierBargeIn,
    LiveInputBuffer,
    notify_caller_interruption,
)


@pytest.mark.asyncio
async def test_barge_in_flushes_only_output_and_never_broadcasts_tool_interruption():
    local = []
    sent = []
    forwarded = []

    async def handle(frame):
        local.append(frame)

    async def send(frame):
        sent.append(frame.message)

    output = SimpleNamespace(_handle_frame=handle, send_message=send, _next_send_time=99)
    processor = LiveCarrierBargeIn(output, "MZfixture")

    async def push(frame, direction):
        forwarded.append((frame, direction))

    processor.push_frame = push
    frame = ProposedUserStartedSpeakingFrame()
    await processor.process_frame(frame, FrameDirection.DOWNSTREAM)
    assert len(local) == 1 and isinstance(local[0], InterruptionFrame)
    assert sent == [{"event": "clear", "streamSid": "MZfixture"}]
    assert output._next_send_time == 0
    assert forwarded == [(frame, FrameDirection.DOWNSTREAM)]
    assert not any(isinstance(f, InterruptionFrame) for f, _ in forwarded)
    await processor.process_frame(ProposedUserStartedSpeakingFrame(), FrameDirection.UPSTREAM)
    assert processor.clear_count == 1
    assert len(local) == 1


def test_startup_trims_only_leading_digital_silence_and_preserves_every_early_word():
    ready = asyncio.Event()
    buffer = LiveInputBuffer(ready, clock=lambda: 12)
    silence = [InputAudioRawFrame(b"\0" * 320, 8000, 1) for _ in range(50)]
    for frame in silence:
        buffer.put_nowait(frame)
    assert buffer.queue.qsize() == 5
    assert buffer.dropped_silence_ms == 900
    speech = InputAudioRawFrame(b"\x10\x01" * 160, 8000, 1)
    buffer.put_nowait(speech)
    after_word = [InputAudioRawFrame(b"\0" * 320, 8000, 1) for _ in range(20)]
    for frame in after_word:
        buffer.put_nowait(frame)
    retained = [buffer.queue.get_nowait() for _ in range(buffer.queue.qsize())]
    assert retained == silence[-5:] + [speech] + after_word
    assert buffer.dropped_silence_ms == 900


def test_live_audio_is_never_trimmed_after_model_readiness():
    ready = asyncio.Event()
    ready.set()
    buffer = LiveInputBuffer(ready, clock=lambda: 15)
    frames = [InputAudioRawFrame(b"\0" * 320, 8000, 1) for _ in range(30)]
    for frame in frames:
        buffer.put_nowait(frame)
    assert buffer.queue.qsize() == 30
    assert buffer.dropped_silence_ms == 0


@pytest.mark.asyncio
async def test_local_speech_clears_and_gates_audio_without_dropping_input_or_tool_results():
    output_events, input_frames, output_frames = [], [], []

    async def handle(frame):
        assert isinstance(frame, InterruptionFrame)

    async def send(frame):
        output_events.append(frame.message)

    output = SimpleNamespace(_handle_frame=handle, send_message=send, _next_send_time=99)
    playback = LiveCarrierBargeIn(output, "MZfixture")

    class Detector:
        def __init__(self):
            self.states = [VADState.SPEAKING, VADState.STOPPING, VADState.QUIET]

        def set_sample_rate(self, rate):
            assert rate == 8000

        async def analyze_audio(self, audio):
            return self.states.pop(0)

    detector = LiveCallerSpeech(playback, vad=Detector())

    async def input_push(frame, direction):
        input_frames.append(frame)

    async def output_push(frame, direction):
        output_frames.append(frame)

    detector.push_frame, playback.push_frame = input_push, output_push
    caller = InputAudioRawFrame(b"\x00\x10" * 160, 8000, 1)
    speech = OutputAudioRawFrame(b"\x00\x10" * 160, 8000, 1)
    tool_result = LLMTextFrame("permitted backend result")
    for _ in range(2):
        await detector.process_frame(caller, FrameDirection.DOWNSTREAM)
        await playback.process_frame(speech, FrameDirection.DOWNSTREAM)
        await playback.process_frame(tool_result, FrameDirection.DOWNSTREAM)
    assert playback.caller_speaking
    assert output_frames == [tool_result, tool_result]
    assert output_events == [{"event": "clear", "streamSid": "MZfixture"}]
    await detector.process_frame(caller, FrameDirection.DOWNSTREAM)
    await playback.process_frame(speech, FrameDirection.DOWNSTREAM)
    assert not playback.caller_speaking
    assert input_frames == [caller, caller, caller]
    assert output_frames[-1] is speech
    assert playback.suppressed_audio_ms == 40
    report = playback.report()
    assert report["received_audio_ms"] == 60
    assert report["forwarded_audio_ms"] == 20
    assert report["suppressed_during_caller_speech_ms"] == 40


@pytest.mark.asyncio
async def test_interruption_correction_uses_supported_event_and_preserves_scope():
    sent = []

    async def send(event):
        sent.append(event.to_payload())

    ready = asyncio.Event()
    llm = SimpleNamespace(ready=ready, closing=False, send_client_event=send)
    assert not await notify_caller_interruption(llm)
    ready.set()
    assert await notify_caller_interruption(llm)
    assert sent[0]["type"] == "session.instructions.append"
    assert sent[0]["delegation_id"] is None
    assert "within the approved scope" in sent[0]["content"]
    llm.closing = True
    assert not await notify_caller_interruption(llm)
    assert len(sent) == 1
