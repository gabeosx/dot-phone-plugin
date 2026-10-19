"""Order Realtime server events behind Pipecat 1.12's native output clear.

The receive task and frame processors run independently. A native interruption
must finish resetting output before the receiver releases a later response's
audio. Server VAD remains the sole turn authority; this adds no turn detector,
audio suppression, or second clear.
"""

from __future__ import annotations

import asyncio
import time

from pipecat.frames.frames import InterruptionFrame, UserStoppedSpeakingFrame
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.openai.realtime import events


class NativeInterruptionOrderingError(RuntimeError):
    """Local output did not settle; reconnecting the model cannot repair it."""


class NativeInterruptionBarrier:
    def __init__(self, llm, output, *, timeout=2.0):
        self.llm, self.output = llm, output
        self.timeout = timeout
        self.pending: asyncio.Future[int] | None = None
        self.pending_stop: asyncio.Future[None] | None = None
        self.expected_frame_id = None
        self.remote_turn_open = False
        self.failed = False
        self.closed = False
        output.add_event_handler("on_after_process_frame", self.output_processed)

    def observe_frame(self, frame, direction):
        if (
            self.pending_stop is not None
            and not self.pending_stop.done()
            and isinstance(frame, UserStoppedSpeakingFrame)
            and direction == FrameDirection.DOWNSTREAM
        ):
            self.pending_stop.set_result(None)
        if (
            self.pending is not None
            and self.expected_frame_id is None
            and isinstance(frame, InterruptionFrame)
            and direction == FrameDirection.DOWNSTREAM
        ):
            # Realtime has one native interruption authority: the upstream user
            # aggregator resolving this server-VAD proposal. Local playback-only
            # clears bypass the LLM and cannot satisfy this exact-frame wait.
            self.expected_frame_id = frame.id

    async def output_processed(self, output, frame):
        if (
            self.pending is not None
            and not self.pending.done()
            and isinstance(frame, InterruptionFrame)
            and frame.id == self.expected_frame_id
        ):
            # FastAPI output has reset its queues, attempted the serialized
            # carrier clear, and reset pacing. This is a local completion signal,
            # not an acknowledgement of playback from the remote carrier.
            self.pending.set_result(int(time.time() * 1000))

    async def speech_started(self, event, handle_start):
        if self.closed or self.failed:
            raise NativeInterruptionOrderingError("native interruption barrier unavailable")
        if self.remote_turn_open:
            # The native turn controller also ignores consecutive starts.
            # Waiting for another clear here would wait for a nonexistent frame.
            return
        self.remote_turn_open = True
        snapshot = self.llm._current_audio_response
        self.llm._current_audio_response = None
        self.pending = asyncio.get_running_loop().create_future()
        self.expected_frame_id = None
        try:
            try:
                async with asyncio.timeout(self.timeout):
                    # Detaching the old response makes the SDK's two truncation
                    # hooks no-ops. Neither may wait for network before clearing.
                    # Only the receive task waits; input dispatch keeps flowing.
                    await handle_start(event)
                    cleared_at_ms = await self.pending
            except TimeoutError as error:
                # voice_failure reconnects model transport TimeoutError. A local
                # output ordering failure must instead terminate the voice path.
                raise NativeInterruptionOrderingError(
                    "native interruption output acknowledgement timed out"
                ) from error
            if snapshot is not None:
                duration_ms = self.llm._calculate_audio_duration_ms(snapshot.total_size)
                truncate_ms = max(0, min(cleared_at_ms - snapshot.start_time_ms, duration_ms))
                async with asyncio.timeout(self.timeout):
                    await self.llm.send_client_event(
                        events.ConversationItemTruncateEvent(
                            item_id=snapshot.item_id,
                            content_index=snapshot.content_index,
                            audio_end_ms=truncate_ms,
                        )
                    )
        except BaseException:
            # Do not admit later response events after an uncertain ordering.
            # Cancellation also poisons this session until its receiver is joined.
            self.failed = True
            raise
        finally:
            if self.pending is not None and not self.pending.done():
                self.pending.cancel()
            self.pending = None
            self.expected_frame_id = None

    async def speech_stopped(self, event, handle_stop):
        """Join the aggregator's stop before a subsequent start can overtake it.

        broadcast_frame only queues the proposal. Without this acknowledgement
        a following start can reach an aggregator whose previous turn is still
        open; it then emits no interruption and the start barrier times out.
        """
        if self.closed or self.failed:
            raise NativeInterruptionOrderingError("native interruption barrier unavailable")
        if not self.remote_turn_open:
            return
        self.pending_stop = asyncio.get_running_loop().create_future()
        try:
            try:
                async with asyncio.timeout(self.timeout):
                    await handle_stop(event)
                    await self.pending_stop
            except TimeoutError as error:
                raise NativeInterruptionOrderingError(
                    "native user turn stop acknowledgement timed out"
                ) from error
            self.remote_turn_open = False
        except BaseException:
            self.failed = True
            raise
        finally:
            if self.pending_stop is not None and not self.pending_stop.done():
                self.pending_stop.cancel()
            self.pending_stop = None

    def reset_session(self):
        """Reset after the old receiver has been cancelled and joined."""
        if self.pending is not None or self.pending_stop is not None or self.closed:
            raise NativeInterruptionOrderingError("interruption barrier still active or closed")
        self.remote_turn_open = False
        self.expected_frame_id = None
        self.failed = False

    def close(self):
        if not self.closed:
            self.closed = True
            self.output.remove_event_handler("on_after_process_frame", self.output_processed)
        if self.pending is not None and not self.pending.done():
            self.pending.cancel()
        if self.pending_stop is not None and not self.pending_stop.done():
            self.pending_stop.cancel()
