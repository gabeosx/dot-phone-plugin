"""Keep live carrier audio current and yield playback without cancelling tools."""

import asyncio
import audioop
import math
import time

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams, VADState
from pipecat.frames.frames import (
    InputAudioRawFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
    OutputTransportMessageUrgentFrame,
    ProposedUserStartedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.openai.live import events


async def notify_caller_interruption(llm):
    if (
        not llm.ready.is_set()
        or getattr(llm, "closing", False)
        or (getattr(getattr(llm, "evidence", None), "close_sent", False))
    ):
        return False
    sender = getattr(llm, "send_supervisor_event", None) or llm.send_client_event
    await sender(
        events.SessionInstructionsAppendEvent(
            delegation_id=None,
            content="Silently discard unfinished speech. Listen to the caller's latest request within the approved scope.",
        )
    )
    return True


class LiveInputBuffer:
    """Preserve early words while retaining only 100 ms of leading digital silence.

    Real carrier audio already arrives in real time. Replaying all pre-ready
    silence at real-time speed otherwise creates a permanent input delay.
    Non-silent audio, and everything following it, is always retained.
    """

    def __init__(self, ready, *, clock=time.monotonic):
        self.ready = ready
        self.clock = clock
        self.queue = asyncio.Queue(maxsize=250)
        self.speech_seen = False
        self.dropped_silence_ms = 0
        self.max_frames = 0
        self.lags = []

    def put_nowait(self, frame):
        frame.metadata.setdefault("live_received_at", self.clock())
        digital_silence = audioop.rms(frame.audio, 2) <= 1
        if not self.ready.is_set() and not self.speech_seen and digital_silence:
            if self.queue.qsize() >= 5:
                dropped = self.queue.get_nowait()
                self.dropped_silence_ms += len(dropped.audio) / (2 * dropped.sample_rate) * 1000
        elif not digital_silence:
            self.speech_seen = True
        self.queue.put_nowait(frame)
        self.max_frames = max(self.max_frames, self.queue.qsize())

    async def get(self):
        return await self.queue.get()

    def dispatched(self, frame):
        self.lags.append((self.clock() - frame.metadata["live_received_at"]) * 1000)

    def report(self):
        ordered = sorted(self.lags)
        return {
            "leading_digital_silence_dropped_ms": round(self.dropped_silence_ms, 2),
            "max_queued_frames": self.max_frames,
            "capture_to_dispatch_p50_ms": round(ordered[len(ordered) // 2], 2) if ordered else None,
            "capture_to_dispatch_p95_ms": round(ordered[math.ceil(len(ordered) * 0.95) - 1], 2)
            if ordered
            else None,
            "capture_to_dispatch_max_ms": round(max(ordered), 2) if ordered else None,
            "boundary": "Carrier PCM received by server to input dispatch completion; excludes provider/network processing",
        }


class LiveCallerSpeech(FrameProcessor):
    """Local speech detection controls playout only; all input still reaches Live."""

    def __init__(self, playback, *, vad=None, on_event=None, on_interrupt=None):
        super().__init__()
        self.playback = playback
        self.on_event = on_event
        self.on_interrupt = on_interrupt
        self.vad = vad or SileroVADAnalyzer(
            sample_rate=8000,
            params=VADParams(confidence=0.7, start_secs=0.1, stop_secs=0.3, min_volume=0.35),
        )
        self.vad.set_sample_rate(8000)

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame) and direction == FrameDirection.DOWNSTREAM:
            frame.metadata.setdefault("live_received_at", time.monotonic())
            if frame.sample_rate != 8000:
                raise ValueError("live carrier speech detector requires 8 kHz mono PCM")
            state = await self.vad.analyze_audio(frame.audio)
            if state == VADState.SPEAKING and not self.playback.caller_speaking:
                self.playback.caller_speaking = True
                if self.on_event:
                    self.on_event("caller_speech_started")
                await self.playback.clear_playback("local_vad")
                if self.on_interrupt:
                    await self.on_interrupt()
            elif state == VADState.QUIET and self.playback.caller_speaking:
                self.playback.caller_speaking = False
                if self.on_event:
                    self.on_event("caller_speech_stopped")
        await self.push_frame(frame, direction)

    async def cleanup(self):
        await self.vad.cleanup()
        await super().cleanup()


class LiveCarrierBargeIn(FrameProcessor):
    def __init__(
        self, output_transport, stream_sid, *, on_event=None, clear_on_projected_turn=True
    ):
        super().__init__()
        self.output_transport = output_transport
        self.stream_sid = stream_sid
        self.clear_count = 0
        self.caller_speaking = False
        self.suppressed_audio_ms = 0
        self.on_event = on_event
        self.clear_reasons = {}
        self.clear_on_projected_turn = clear_on_projected_turn
        self.pipeline_interruptions = 0
        self.received_audio_ms = 0
        self.forwarded_audio_ms = 0

    async def clear_playback(self, reason):
        # This pinned hook flushes only the output transport's local queue.
        # No InterruptionFrame is broadcast to the LLM or backend tools.
        await self.output_transport._handle_frame(InterruptionFrame())
        await self.output_transport.send_message(
            OutputTransportMessageUrgentFrame(
                message={"event": "clear", "streamSid": self.stream_sid}
            )
        )
        self.output_transport._next_send_time = 0
        self.clear_count += 1
        self.clear_reasons[reason] = self.clear_reasons.get(reason, 0) + 1
        if self.on_event:
            self.on_event("playback_cleared", reason=reason)

    def report(self):
        return {
            "clear_reasons": dict(self.clear_reasons),
            "pipeline_interruptions": self.pipeline_interruptions,
            "received_audio_ms": round(self.received_audio_ms, 2),
            "forwarded_audio_ms": round(self.forwarded_audio_ms, 2),
            "suppressed_during_caller_speech_ms": round(self.suppressed_audio_ms, 2),
            "boundary": "PCM at playback gate; forwarded audio can still be interrupted before carrier send",
        }

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if (
            isinstance(frame, ProposedUserStartedSpeakingFrame)
            and direction == FrameDirection.DOWNSTREAM
            and self.clear_on_projected_turn
        ):
            await self.clear_playback("projected_turn")
        if isinstance(frame, InterruptionFrame) and direction == FrameDirection.DOWNSTREAM:
            self.pipeline_interruptions += 1
        if isinstance(frame, OutputAudioRawFrame) and direction == FrameDirection.DOWNSTREAM:
            duration_ms = len(frame.audio) / (2 * frame.sample_rate * frame.num_channels) * 1000
            self.received_audio_ms += duration_ms
            if self.caller_speaking:
                self.suppressed_audio_ms += duration_ms
                return
            self.forwarded_audio_ms += duration_ms
        await self.push_frame(frame, direction)
