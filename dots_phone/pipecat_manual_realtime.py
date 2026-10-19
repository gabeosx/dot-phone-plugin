"""Opt-in external turn ownership, pinned to Pipecat 1.12 queued transports.

No speech detector or endpoint timeout is supplied here. A separately injected,
per-call source owns decisions. Model transport loss fails closed: replacing a
session without an explicit source-reset protocol would leave ownership unclear.
"""

import asyncio
import base64
import contextvars
import time
from dataclasses import dataclass

from pipecat.audio.resamplers.soxr_stream_resampler import SOXRStreamAudioResampler
from pipecat.frames.frames import (
    InputAudioRawFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMServiceMetadataFrame,
    LLMTextFrame,
    ProposedUserStartedSpeakingFrame,
    ProposedUserStoppedSpeakingFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TTSTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor, FrameProcessorQueue
from pipecat.services.openai.realtime import events
from pipecat.turns.user_turn_strategies import ExternalUserTurnStrategies

from dots_phone.pipecat_realtime import SupervisedRealtimeService
from dots_phone.pipecat_turn_control import ManualTurnControlError, ManualTurnController

_source_response = contextvars.ContextVar("manual_source_response", default=None)
_source_item = contextvars.ContextVar("manual_source_item", default=None)
_context_response = contextvars.ContextVar("manual_context_response", default=None)


@dataclass(frozen=True)
class OutputCursor:
    response_id: str
    item_id: str
    content_index: int
    start_time_ms: int
    admitted_bytes: int


class ManualRealtimeService(SupervisedRealtimeService):
    """One external authority, ordered raw input, and generation-owned output."""

    def __init__(self, **kwargs):
        kwargs["settings"].session_properties.audio.input.turn_detection = False
        super().__init__(**kwargs)
        self.input_resampler = SOXRStreamAudioResampler(clear_after_secs=None)
        self.snapshots = {}
        self.turn_epoch = 0
        self._external_closed = False
        self._proposed_open = False
        self._proposal_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._manual_close_task = None
        self._proposal_tasks = set()
        self._accepted_manual = False
        self._playback_closed = False
        self._proposal_ack = None
        self._expected_boundary = None
        self._expected_interruption_id = None
        self.captured_input_samples = 0
        self._stop_projections = {}
        self._turn_recipient_numbers = {}
        self.stale_delta_bytes = self.stale_handoff_bytes = 0
        self.stale_admission_bytes = 0
        self.admitted_pcm = {}
        self.output = None
        self._user_aggregator = None
        self._delta_lock = asyncio.Lock()
        self._deferred_bot_stop = None
        self.manual = ManualTurnController(
            append=self.append_input,
            flush=self.flush_input,
            interrupt=self.cancel_old,
            commit=self.commit_input,
            create=self.create_manual_response,
            fail=self.fail_manual,
            ready=self.ready,
        )

    def service_metadata_frame(self):
        # The injected external source is the sole authority. Do not instantiate an
        # implicit local detector or let transcription create another turn.
        return LLMServiceMetadataFrame(
            service_name=self.name,
            is_realtime_service=True,
            user_turn_strategies=ExternalUserTurnStrategies(),
        )

    def bind_response_aggregators(self, user, assistant):
        """Guard paired context mutations when queued response markers arrive."""
        if self._user_aggregator is not None:
            raise RuntimeError("Manual response aggregators already bound")
        self._user_aggregator = user

        def guard(handler):
            async def consume(frame):
                owner = frame.metadata.get("manual_generation")
                if owner is None or not self.manual.allows_audio(owner[0]):
                    return
                token = _context_response.set(owner[0])
                try:
                    await handler(frame)
                finally:
                    _context_response.reset(token)

            return consume

        for name in ("_handle_llm_start", "_handle_llm_end", "_handle_tts_started", "_handle_text"):
            setattr(assistant, name, guard(getattr(assistant, name)))

        def guard_flush(handler):
            async def flush():
                # The pinned original next schedules/mutates aggregation.
                # Recheck ownership after the cancellation await has settled.
                await user._cancel_realtime_handoff_flush_task()
                owner = _context_response.get()
                if owner is not None and self.manual.allows_audio(owner):
                    await handler()

            return flush

        user._realtime_handoff_flush = guard_flush(user._realtime_handoff_flush)
        user._realtime_handoff_flush_immediate = guard_flush(user._realtime_handoff_flush_immediate)

    def bind_output_transport(self, output):
        if self.output is not None:
            raise RuntimeError("Manual output transport already bound")
        self.output = output
        original_queue = output.queue_frame
        original_handle = output._handle_frame
        controller = self.manual
        binding = {"active": True}

        async def admit(frame, direction=FrameDirection.DOWNSTREAM, callback=None):
            owner = frame.metadata.get("manual_generation")
            if owner is None or direction != FrameDirection.DOWNSTREAM:
                return await original_queue(frame, direction, callback)
            if output._enable_direct_mode:
                raise RuntimeError("Manual turn admission requires queued mode")
            queue = output._FrameProcessor__input_queue
            if not isinstance(queue, FrameProcessorQueue) or queue.maxsize != 0:
                raise RuntimeError("Unsupported pinned output input queue")
            if not binding["active"] or output._cancelling or not controller.allows_audio(owner[0]):
                if isinstance(frame, TTSAudioRawFrame):
                    self.stale_admission_bytes += len(frame.audio)
                return
            if isinstance(frame, TTSAudioRawFrame) and owner not in self.admitted_pcm:
                if len(self.admitted_pcm) >= 512:
                    self.voice_failure("external_output_history_full", ManualTurnControlError())
                    return
            # This is the final output processor's actual queue admission,
            # after upstream before-push and observer hooks. Both ownership
            # check and enqueue are non-yielding. Reproduce the pinned 1.12
            # default-priority tuple; Start/System frames use the original path.
            queue._FrameProcessorQueue__counter += 1
            queue.put_nowait(
                (
                    queue.DEFAULT_PRIORITY,
                    queue._FrameProcessorQueue__counter,
                    (frame, direction, callback),
                )
            )
            if isinstance(frame, TTSAudioRawFrame):
                self.admitted_pcm[owner] = self.admitted_pcm.get(owner, 0) + len(frame.audio)

        output.queue_frame = admit

        async def route(frame):
            # A priority clear can overtake audio still in the processor input
            # queue. Recheck after dequeue/process observers, before media routing.
            owner = frame.metadata.get("manual_generation")
            if owner is not None and (
                not binding["active"] or not controller.allows_audio(owner[0])
            ):
                return
            await original_handle(frame)

        output._handle_frame = route
        self._output_binding = (binding, original_queue, admit, original_handle, route)
        output.add_event_handler("on_after_process_frame", self.output_processed)

    def unbind_output_transport(self):
        if self.output is not None:
            binding, *_ = self._output_binding
            binding["active"] = False
            self.output.remove_event_handler("on_after_process_frame", self.output_processed)
            # Model disconnect can precede output task cleanup. Keep inactive
            # guards on this per-call output until it is discarded: restoring
            # the original methods could release already-queued old audio.
            self.output = None

    async def output_processed(self, output, frame):
        if isinstance(frame, InterruptionFrame):
            self.manual.output_cleared(frame.id, int(time.time() * 1000))
            if (
                self._expected_boundary == "start"
                and self._proposal_ack is not None
                and frame.id == self._expected_interruption_id
            ):
                if not self._proposal_ack.done():
                    self._proposal_ack.set_result(True)

    def recognize_onset(self, *, epoch: int, turn_id: int) -> bool:
        """Immediately revoke a settled generation when local speech is known.

        A preceding stop may still be projecting, and its acknowledgement can
        sit behind slow VAD work in the source FIFO. Do not let either delay
        permit a reply or queued audio after the source recognized new speech.
        An unadmitted stop still belongs to the same open caller turn; its
        eventual deferral must leave that turn able to answer its real endpoint.
        """
        if epoch != self.turn_epoch or turn_id != self.manual.turn_id or self._external_closed:
            return False
        if not self.manual.speaking:
            return self.manual.revoke_response(turn_id)
        return True

    async def propose_turn(
        self, *, started: bool, epoch: int, turn_id: int, classified_samples: int | None = None
    ) -> bool:
        """Apply one external decision through the user aggregator's lifecycle.

        The source captures turn_epoch and manual.turn_id before its asynchronous decision starts.
        Old jobs cannot affect a closed/replaced ownership epoch. This method
        contains no endpoint inference and no timer that finishes caller speech.
        Local sources also supply the exact classified input frontier for stops.
        Omitting it preserves the original custom-source admission contract.
        """
        if epoch != self.turn_epoch or turn_id != self.manual.turn_id or self._external_closed:
            return False
        if len(self._proposal_tasks) >= 8:
            self.voice_failure("external_proposal_overflow", ManualTurnControlError())
            return False
        # Upstream interruption can cancel the source processor's data task.
        # Its bounded ownership handshake must finish independently; close joins it.
        task = asyncio.create_task(
            self._apply_turn(
                started=started, epoch=epoch, turn_id=turn_id, classified_samples=classified_samples
            )
        )
        self._proposal_tasks.add(task)
        task.add_done_callback(self._proposal_tasks.discard)
        return await asyncio.shield(task)

    async def _apply_turn(self, *, started, epoch, turn_id, classified_samples=None):
        try:
            await asyncio.wait_for(self.ready.wait(), 10)
        except TimeoutError:
            self.voice_failure("external_not_ready", ManualTurnControlError())
            return False
        async with self._proposal_lock:
            if (
                not self._accepted_manual
                or epoch != self.turn_epoch
                or turn_id != self.manual.turn_id
                or self._external_closed
                or started == self._proposed_open
            ):
                return False
            if (
                not started
                and classified_samples is not None
                and classified_samples != self.captured_input_samples
            ):
                # PCM may have arrived while readiness or a prior projection
                # was pending. Keep this same turn open for new source evidence.
                return False
            self._proposed_open = started
            self._expected_boundary = "start" if started else "stop"
            self._proposal_ack = asyncio.get_running_loop().create_future()
            if not started and classified_samples is not None:
                # Check and place the stop in the input FIFO without yielding.
                # Later captured PCM must remain behind this validated cutoff,
                # even if aggregator projection is delayed. The commit waits
                # for this exact projection below before it reaches the wire.
                self._stop_projections[turn_id] = self._proposal_ack
                self.manual.end_turn()
            if started:
                self._expected_interruption_id = None
                turn = self.manual.begin_turn()
                current = self._current_audio_response
                self.snapshots[turn] = (
                    None
                    if current is None
                    else OutputCursor(
                        response_id=current.response_id,
                        item_id=current.item_id,
                        content_index=current.content_index,
                        start_time_ms=current.start_time_ms,
                        admitted_bytes=self.admitted_pcm.get(
                            (current.response_id, current.item_id, current.content_index), 0
                        ),
                    )
                )
                self.caller_speaking = True
                self.reasoner_turn += 1
                if self._user_aggregator is not None:
                    await self._user_aggregator._cancel_realtime_handoff_flush_task()
                call = self.supervisor.store.get("call", self.call_id)
                if call:
                    number = call.get("recipient_audio_turn", 0) + 1
                    call["recipient_audio_turn"] = number
                    self._turn_recipient_numbers[turn] = number
                    self.supervisor.save(call)
            try:
                async with asyncio.timeout(2):
                    await self.broadcast_frame(
                        ProposedUserStartedSpeakingFrame
                        if started
                        else ProposedUserStoppedSpeakingFrame
                    )
                    return await self._proposal_ack
            except TimeoutError:
                self.voice_failure(
                    "external_turn_projection",
                    ManualTurnControlError("external turn projection did not settle"),
                )
                return False
            finally:
                self._proposal_ack = None
                self._expected_boundary = None

    async def process_frame(self, frame, direction):
        if isinstance(frame, InterruptionFrame) and direction == FrameDirection.DOWNSTREAM:
            if self._expected_boundary != "start" or not self.manual.speaking:
                self.voice_failure("external_unowned_interruption", ManualTurnControlError())
                return
            if self._expected_interruption_id is not None:
                self.voice_failure("external_duplicate_interruption", ManualTurnControlError())
                return
            self._expected_interruption_id = frame.id
            self.manual.begin_turn(frame.id)
        await super().process_frame(frame, direction)

    async def _handle_interruption(self):
        # The FIFO owns cancel/truncate, after the output clear. Never invoke
        # SDK manual clear+pre-roll, which races asynchronous input appends.
        await self.stop_all_metrics()

    async def _handle_user_started_speaking(self, frame):
        if self._expected_boundary != "start":
            self.voice_failure("external_unowned_start", ManualTurnControlError())

    async def _handle_user_stopped_speaking(self, frame):
        if self._expected_boundary != "stop":
            self.voice_failure("external_unowned_stop", ManualTurnControlError())
            return
        self.caller_speaking = False
        self.caller_stopped_at = time.monotonic()
        self.manual.end_turn()
        if self._proposal_ack is not None and not self._proposal_ack.done():
            self._proposal_ack.set_result(True)

    async def _handle_evt_speech_started(self, event):
        self.voice_failure("external_native_turn", ManualTurnControlError())

    async def _handle_evt_speech_stopped(self, event):
        self.voice_failure("external_native_turn", ManualTurnControlError())

    async def accept_input(self, frame):
        """Capture once before a source can emit priority turn-boundary frames."""
        if frame.metadata.get("manual_input_accepted") or self._external_closed:
            return
        if frame.sample_rate != 8000 or frame.num_channels != 1 or len(frame.audio) % 2:
            self.voice_failure("external_input_format", ManualTurnControlError())
            return
        frame.metadata["manual_input_accepted"] = True
        self.captured_input_samples += len(frame.audio) // 2
        frame.metadata["manual_input_sample_end"] = self.captured_input_samples
        duration = round(len(frame.audio) / (2 * frame.sample_rate) * 1000)
        self.media_progress["input_frames_received"] += 1
        self.media_progress["input_ms_received"] += duration
        self.media_progress["last_input_received_at_ms"] = round(
            (time.monotonic() - self.control_clock) * 1000
        )
        # Source processors may attach annotations or reuse frame objects. The
        # queued immutable PCM snapshot remains the original captured input.
        self.manual.enqueue_audio(
            InputAudioRawFrame(bytes(frame.audio), frame.sample_rate, frame.num_channels)
        )

    async def _send_user_audio(self, frame):
        if not frame.metadata.get("manual_input_accepted") and not self._external_closed:
            self.voice_failure("external_capture_missing", ManualTurnControlError())

    async def append_input(self, frame):
        audio = await self.input_resampler.resample(frame.audio, frame.sample_rate, 24000)
        await self.append_pcm(audio)
        self.media_progress["input_frames_dispatched"] += 1
        self.media_progress["input_ms_dispatched"] += round(
            len(frame.audio) / (2 * frame.sample_rate) * 1000
        )
        self.media_progress["last_input_dispatched_at_ms"] = round(
            (time.monotonic() - self.control_clock) * 1000
        )

    async def append_pcm(self, audio):
        if audio:
            await self.send_client_event(
                events.InputAudioBufferAppendEvent(audio=base64.b64encode(audio).decode())
            )

    async def flush_input(self):
        tail = await self.input_resampler.flush()
        await self.append_pcm(tail)

    async def cancel_old(self, turn, cleared_at):
        snapshot = self.snapshots.pop(turn, None)
        # The pinned SDK retains a global response reference across its
        # TTSStartedFrame await, then dereferences it again. Clear output now,
        # but detach that SDK reference only when its delta handler has exited.
        async with self._delta_lock:
            if (
                snapshot is not None
                and self._current_audio_response is not None
                and self._current_audio_response.response_id == snapshot.response_id
            ):
                self._current_audio_response = None
        await self.send_client_event(events.ResponseCancelEvent())
        if snapshot is not None:
            await self.send_client_event(
                events.ConversationItemTruncateEvent(
                    item_id=snapshot.item_id,
                    content_index=snapshot.content_index,
                    audio_end_ms=max(
                        0,
                        min(
                            cleared_at - snapshot.start_time_ms,
                            self._calculate_audio_duration_ms(snapshot.admitted_bytes),
                        ),
                    ),
                )
            )

    async def commit_input(self):
        projection = self._stop_projections.pop(self.manual.committing_turn, None)
        if projection is not None and not await asyncio.shield(projection):
            raise ManualTurnControlError("manual stop projection revoked")
        await self.send_client_event(events.InputAudioBufferCommitEvent())

    async def create_manual_response(self, turn):
        await self.manual.send_response(
            turn,
            lambda: super(ManualRealtimeService, self).send_client_event(
                events.ResponseCreateEvent()
            ),
        )

    async def fail_manual(self, error):
        self.voice_failure("external_turn_control", ManualTurnControlError(type(error).__name__))

    async def _handle_bot_stopped_speaking(self):
        if self._delta_lock.locked():
            # Keep the ingress processor running while a delta send yields.
            self._deferred_bot_stop = self._current_audio_response
            return
        await super()._handle_bot_stopped_speaking()

    async def send_client_event(self, event):
        if isinstance(event, events.ResponseCreateEvent):
            # All create paths, including tool/final-report continuations, use
            # the same gate. Superseded requests coalesce into the next turn;
            # tool results already delivered remain in provider context.
            turn = self.manual.turn_id
            await self.manual.send_response(
                turn, lambda: super(ManualRealtimeService, self).send_client_event(event)
            )
            return
        await super().send_client_event(event)

    async def observe(self, event):
        announce_response = (
            event["type"] == "response.created"
            and event["response"]["id"] not in self.responses_created
        )
        if event["type"] == "session.updated":
            audio_input = (event.get("session", {}).get("audio") or {}).get("input") or {}
            self._accepted_manual = (
                "turn_detection" in audio_input and audio_input["turn_detection"] is None
            )
            if not self._accepted_manual:
                self.voice_failure("external_native_detection_enabled", ManualTurnControlError())
                return
        if event["type"] in {
            "input_audio_buffer.speech_started",
            "input_audio_buffer.speech_stopped",
        }:
            self.voice_failure("external_native_turn", ManualTurnControlError())
            return
        if event["type"] == "input_audio_buffer.committed":
            item_id = event.get("item_id")
            number = self._turn_recipient_numbers.pop(self.manual.committing_turn, None)
            if item_id and number is not None:
                self.recipient_audio_items[item_id] = number
                if len(self.recipient_audio_items) > 256:
                    self.recipient_audio_items.pop(next(iter(self.recipient_audio_items)))
            self.manual.input_committed()
        elif event["type"] == "response.created":
            self.manual.response_created(event["response"]["id"])
        elif event["type"] == "response.done":
            self.manual.response_done(event["response"]["id"])
        await super().observe(event)
        if announce_response:
            response_id = event["response"]["id"]
            token = _source_response.set(response_id)
            item_token = _source_item.set((response_id, "", 0))
            try:
                await self.push_frame(LLMFullResponseStartFrame())
            finally:
                _source_item.reset(item_token)
                _source_response.reset(token)

    async def _handle_evt_session_updated(self, event):
        if self._accepted_manual and not self._external_closed:
            await super()._handle_evt_session_updated(event)

    async def _handle_evt_response_done(self, event):
        token = _source_response.set(event.response.id)
        item_token = _source_item.set((event.response.id, "", 0))
        try:
            await super()._handle_evt_response_done(event)
        finally:
            _source_item.reset(item_token)
            _source_response.reset(token)

    async def _owned_text_event(self, event, handler):
        if not self.manual.allows_audio(event.response_id):
            return
        token = _source_response.set(event.response_id)
        item_token = _source_item.set((event.response_id, event.item_id, event.content_index))
        try:
            await handler(event)
        finally:
            _source_item.reset(item_token)
            _source_response.reset(token)

    async def _handle_evt_audio_transcript_delta(self, event):
        await self._owned_text_event(event, super()._handle_evt_audio_transcript_delta)

    async def _handle_evt_text_delta(self, event):
        await self._owned_text_event(event, super()._handle_evt_text_delta)

    async def _handle_evt_audio_delta(self, event):
        if not self.manual.allows_audio(event.response_id):
            self.stale_delta_bytes += len(base64.b64decode(event.delta))
            return
        token = _source_response.set(event.response_id)
        item_token = _source_item.set((event.response_id, event.item_id, event.content_index))
        try:
            async with self._delta_lock:
                # A queued delta can lose eligibility while waiting for the
                # SDK state owner; never admit it on an earlier observation.
                if not self.manual.allows_audio(event.response_id):
                    self.stale_delta_bytes += len(base64.b64decode(event.delta))
                    return
                try:
                    await super()._handle_evt_audio_delta(event)
                finally:
                    if (
                        self._deferred_bot_stop is not None
                        and self._current_audio_response is self._deferred_bot_stop
                    ):
                        self._current_audio_response = None
                    self._deferred_bot_stop = None
        finally:
            _source_item.reset(item_token)
            _source_response.reset(token)

    async def push_frame(self, frame, direction=FrameDirection.DOWNSTREAM):
        response_id = _source_response.get()
        if isinstance(frame, LLMFullResponseStartFrame) and response_id is None:
            # SDK placeholder/item-added starts lack a response ID. The exact
            # response.created event owns the authoritative context boundary.
            return
        if (
            isinstance(
                frame,
                (
                    TTSStartedFrame,
                    TTSStoppedFrame,
                    TTSAudioRawFrame,
                    LLMFullResponseEndFrame,
                    LLMFullResponseStartFrame,
                    LLMTextFrame,
                    TTSTextFrame,
                ),
            )
            and response_id is not None
        ):
            frame.metadata["manual_generation"] = _source_item.get()
            if not self.manual.allows_audio(response_id):
                if isinstance(frame, TTSAudioRawFrame):
                    self.stale_handoff_bytes += len(frame.audio)
                return
        await super().push_frame(frame, direction)

    def voice_failure(self, phase, error=None):
        # A replacement model cannot preserve the external source's causal
        # state. Native mode keeps its existing reconnect policy unchanged.
        if hasattr(self, "manual"):
            self._invalidate_external_turns()
            if self._manual_close_task is None:
                self._manual_close_task = asyncio.create_task(self.close_external_turns())
        super().voice_failure(
            phase,
            ManualTurnControlError(
                type(error).__name__ if error is not None else "manual voice failure"
            ),
        )

    def _invalidate_external_turns(self):
        if not self._external_closed:
            self._external_closed = True
            self.turn_epoch += 1
            self._proposed_open = False
        if self._proposal_ack is not None and not self._proposal_ack.done():
            self._proposal_ack.set_result(False)
        # Synchronously revoke audio before async close joins queued work.
        self.manual.closed = True

    async def close_external_turns(self):
        self._invalidate_external_turns()
        async with self._close_lock:
            tasks = [task for task in self._proposal_tasks if task is not asyncio.current_task()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.manual.close()
            if not self._playback_closed:
                self._playback_closed = True
                if self.clear_playback is not None:
                    try:
                        async with asyncio.timeout(2):
                            await self.clear_playback("external_turn_closed")
                    except Exception:
                        super().voice_failure("external_clear_failed", ManualTurnControlError())
            self.snapshots.clear()
            self.admitted_pcm.clear()
            self._turn_recipient_numbers.clear()
            self._stop_projections.clear()

    async def reset_interruption_session(self):
        await self.close_external_turns()
        self.voice_failure("external_session_reset", ManualTurnControlError())

    async def recover_transport(self, error):
        await self.close_external_turns()
        self.recovering_transport = False
        self.voice_failure("external_transport_lost", ManualTurnControlError())

    async def stop(self, frame):
        await self.close_external_turns()
        await super().stop(frame)

    async def cancel(self, frame):
        await self.close_external_turns()
        await super().cancel(frame)

    async def cleanup(self):
        await self.close_external_turns()
        await super().cleanup()

    async def _disconnect(self):
        await self.close_external_turns()
        await super()._disconnect()


class ManualInputCapture(FrameProcessor):
    """Capture input before priority source decisions can overtake PCM frames."""

    def __init__(self, llm: ManualRealtimeService):
        super().__init__()
        self.llm = llm

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame) and direction == FrameDirection.DOWNSTREAM:
            await self.llm.accept_input(frame)
        await self.push_frame(frame, direction)
