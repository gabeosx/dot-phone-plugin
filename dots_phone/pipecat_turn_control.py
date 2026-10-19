"""Ordered input and playback ownership for opt-in local Realtime turns.

This module has no detector, timers that finish turns, SDK imports, or audio
filter. The upstream turn controller is the sole authority. Callbacks perform
the existing service operations; all input operations share one bounded FIFO.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


class ManualTurnControlError(RuntimeError):
    """An ordering boundary failed; do not continue uncertain playback."""


@dataclass(frozen=True)
class _Command:
    kind: str
    value: Any


class ManualTurnController:
    """Serialize captured audio, interruption RPCs, and confirmed turn ends.

    Integration contract:
    * enqueue_audio receives every input frame, once, in arrival order.
    * begin_turn receives the exact downstream InterruptionFrame.id BEFORE it
      is forwarded. It returns immediately; forwarding must never wait here.
    * output_cleared is called only by that output's after-process hook.
    * interrupt cancels/truncates the old response, but MUST NOT clear input or
      replay pre-roll. Previously appended input remains intact.
    * flush appends only the real resampler tail; commit sends one commit.
      input_committed acknowledges it from the model receive task.
    * EVERY response.create goes through response_requested BEFORE its send;
      response_created/done come from the model receive task. Generation done
      does not revoke queued audio. Recognized onset revokes current playback
      before the following asynchronous turn projection advances its identity.
    * close must complete before constructing a replacement controller. Old
      callbacks must retain the old instance and cannot acknowledge the new one.
    """

    def __init__(
        self,
        *,
        append: Callable[[Any], Awaitable[None]],
        flush: Callable[[], Awaitable[None]],
        interrupt: Callable[[int, int], Awaitable[None]],
        commit: Callable[[], Awaitable[None]],
        create: Callable[[int], Awaitable[None]],
        fail: Callable[[Exception], Awaitable[None]],
        timeout: float = 2.0,
        max_queue: int = 250,
        max_responses: int = 256,
        ready: asyncio.Event | None = None,
        ready_timeout: float = 10.0,
    ):
        self._append, self._flush = append, flush
        self._interrupt, self._commit, self._create = interrupt, commit, create
        self._fail_callback = fail
        self._timeout, self._max_responses = timeout, max_responses
        self._ready = ready
        self._ready_timeout = ready_timeout
        self._create_lock = asyncio.Lock()
        self._queue: asyncio.Queue[_Command] = asyncio.Queue(maxsize=max_queue)
        self._worker: asyncio.Task | None = None
        self._failure_task: asyncio.Task | None = None
        self._clear_waits: dict[int | None, asyncio.Future[int]] = {}
        self._commit_wait: asyncio.Future[None] | None = None
        self._requested: deque[int] = deque()
        self._responses: dict[str, int] = {}
        self._active: set[str] = set()
        self._response_changed = asyncio.Event()
        self.turn_id = 0
        self._settled_turn = 0
        self._committed_turn = 0
        self._revoked_response_turn: int | None = None
        self.committing_turn: int | None = None
        self.speaking = False
        self.closed = False
        self.failure: Exception | None = None
        self.audio_frames = 0
        self.commits = 0
        self.response_requests = 0

    @property
    def pending_commands(self) -> int:
        return self._queue.qsize()

    def _put(self, kind: str, value=None):
        if self.closed or self.failure is not None:
            return
        try:
            self._queue.put_nowait(_Command(kind, value))
        except asyncio.QueueFull:
            self._failed(ManualTurnControlError("manual input queue exceeded its bound"))
            return
        if self._worker is None:
            self._worker = asyncio.create_task(self._run(), name="manual-turn-input")

    def enqueue_audio(self, frame):
        self._put("audio", frame)

    def begin_turn(self, interruption_frame_id: int | None = None) -> int:
        """Invalidate old playback immediately, then enqueue the input boundary."""
        if self.closed or self.failure is not None:
            return self.turn_id
        if self.speaking:
            if interruption_frame_id is not None and None in self._clear_waits:
                self._clear_waits[interruption_frame_id] = self._clear_waits.pop(None)
            return self.turn_id
        self.turn_id += 1
        self.speaking = True
        self._response_changed.set()
        pending = asyncio.get_running_loop().create_future()
        self._clear_waits[interruption_frame_id] = pending
        self._put("start", (self.turn_id, pending))
        return self.turn_id

    def output_cleared(self, interruption_frame_id: int, at_ms: int):
        pending = self._clear_waits.get(interruption_frame_id)
        if pending is not None and not pending.done() and not self.closed:
            pending.set_result(at_ms)

    def end_turn(self):
        """Enqueue only a detector-confirmed end, once for the current turn."""
        if self.closed or self.failure is not None or not self.speaking:
            return
        self.speaking = False
        self._put("stop", self.turn_id)

    def input_committed(self):
        pending = self._commit_wait
        if pending is not None and not pending.done() and not self.closed:
            pending.set_result(None)

    def revoke_response(self, turn_id: int) -> bool:
        """Revoke recognized-onset playback before asynchronous start projection.

        Keep the generation unchanged so an in-flight stop acknowledgement and
        the following begin_turn still refer to their original turn identities.
        """
        if self.closed or self.failure is not None or turn_id != self.turn_id:
            return False
        self._revoked_response_turn = turn_id
        self._response_changed.set()
        return True

    def can_respond(self, turn_id: int) -> bool:
        return (
            not self.closed
            and self.failure is None
            and not self.speaking
            and turn_id != self._revoked_response_turn
            and turn_id == self.turn_id
            and turn_id == self._settled_turn
            and turn_id == self._committed_turn
        )

    async def send_response(self, turn_id: int, send: Callable[[], Awaitable[None]]) -> bool:
        """Serialize every create send, registering its fixed generation first.

        The callback sends exactly one response.create. It must not recursively
        register/request a response. A superseded request is deliberately skipped.
        """
        async with self._create_lock:
            if not self.can_respond(turn_id) or self._requested or self._active:
                return False
            self.response_requested(turn_id)
            try:
                async with asyncio.timeout(self._timeout):
                    await send()
            except Exception as error:
                self._failed(error)
                raise
            return True

    def response_requested(self, turn_id: int | None = None):
        """Call immediately before every response.create, including startup."""
        if self.closed or self.failure is not None:
            raise ManualTurnControlError("manual turn controller unavailable")
        if turn_id is None:
            turn_id = self.turn_id
        if not self.can_respond(turn_id):
            raise ManualTurnControlError("response requested for an open or superseded turn")
        if len(self._requested) >= self._max_responses:
            error = ManualTurnControlError("manual response request bound exceeded")
            self._failed(error)
            raise error
        self._requested.append(turn_id)
        self.response_requests += 1
        self._response_changed.set()

    def response_created(self, response_id: str):
        if response_id in self._responses:
            return
        if not self._requested:
            self._failed(ManualTurnControlError("unrequested manual response"))
            return
        if len(self._responses) >= self._max_responses:
            self._failed(ManualTurnControlError("manual response history bound exceeded"))
            return
        self._responses[response_id] = self._requested.popleft()
        self._active.add(response_id)
        self._response_changed.set()

    def response_done(self, response_id: str):
        # Keep its generation: model generation can finish long before the
        # carrier consumes queued audio. Recognized onset or a later turn
        # invalidates it, rather than generation completion.
        self._active.discard(response_id)
        self._response_changed.set()

    def allows_audio(self, response_id: str) -> bool:
        return (
            not self.closed
            and self.failure is None
            and not self.speaking
            and self.turn_id != self._revoked_response_turn
            and self._responses.get(response_id) == self.turn_id
            and self.turn_id == self._settled_turn
            and self.turn_id == self._committed_turn
        )

    async def _run(self):
        try:
            while not self.closed and self.failure is None:
                command = await self._queue.get()
                try:
                    if self._ready is not None:
                        async with asyncio.timeout(self._ready_timeout):
                            await self._ready.wait()
                    async with asyncio.timeout(self._timeout):
                        if command.kind == "audio":
                            await self._append(command.value)
                            self.audio_frames += 1
                        elif command.kind == "start":
                            turn_id, pending = command.value
                            cleared_at = await pending
                            self._clear_waits = {
                                key: value
                                for key, value in self._clear_waits.items()
                                if value is not pending
                            }
                            await self._interrupt(turn_id, cleared_at)
                            self._settled_turn = turn_id
                        else:
                            turn_id = command.value
                            await self._flush()
                            self._commit_wait = asyncio.get_running_loop().create_future()
                            self.committing_turn = turn_id
                            try:
                                await self._commit()
                                self.commits += 1
                                await self._commit_wait
                                self._committed_turn = turn_id
                            finally:
                                if self._commit_wait and not self._commit_wait.done():
                                    self._commit_wait.cancel()
                                self._commit_wait = None
                                self.committing_turn = None
                            # New speech may have arrived while network sends
                            # yielded. Its input stays queued, and the old end
                            # may commit but must never start fresh playback.
                            while (self._active or self._requested) and self.can_respond(turn_id):
                                self._response_changed.clear()
                                await self._response_changed.wait()
                            if self.can_respond(turn_id):
                                await self._create(turn_id)
                finally:
                    self._queue.task_done()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._failed(error)

    def _failed(self, error: Exception):
        if self.failure is not None or self.closed:
            return
        self.failure = error
        for pending in self._clear_waits.values():
            if not pending.done():
                pending.cancel()
        if self._commit_wait is not None and not self._commit_wait.done():
            self._commit_wait.cancel()
        if self._worker is not None and self._worker is not asyncio.current_task():
            self._worker.cancel()
        while not self._queue.empty():
            self._queue.get_nowait()
            self._queue.task_done()
        self._failure_task = asyncio.create_task(self._fail_callback(error))

    async def wait_idle(self):
        """Testing/teardown aid; never use this as a live turn-end boundary."""
        await self._queue.join()

    async def close(self):
        self.closed = True
        for pending in self._clear_waits.values():
            if not pending.done():
                pending.cancel()
        if self._commit_wait is not None and not self._commit_wait.done():
            self._commit_wait.cancel()
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None
        while not self._queue.empty():
            self._queue.get_nowait()
            self._queue.task_done()
        self._clear_waits.clear()
        self._requested.clear()
        self._responses.clear()
        self._active.clear()
        if self._failure_task is not None and self._failure_task is not asyncio.current_task():
            self._failure_task.cancel()
            await asyncio.gather(self._failure_task, return_exceptions=True)
