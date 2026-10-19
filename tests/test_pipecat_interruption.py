"""No-network lifecycle regressions for native interruption ordering."""

import asyncio
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("pipecat")

from pipecat.frames.frames import InterruptionFrame, UserStoppedSpeakingFrame  # noqa: E402
from pipecat.processors.frame_processor import FrameDirection  # noqa: E402

from dots_phone.pipecat_interruption import (  # noqa: E402
    NativeInterruptionBarrier,
    NativeInterruptionOrderingError,
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Interruption lifecycle tests must not use network connections")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


class FakeOutput:
    def __init__(self):
        self.handlers = []

    def add_event_handler(self, event, callback):
        assert event == "on_after_process_frame"
        self.handlers.append(callback)

    def remove_event_handler(self, event, callback):
        assert event == "on_after_process_frame"
        self.handlers.remove(callback)

    async def processed(self, frame):
        for handler in self.handlers[:]:
            await handler(self, frame)


@pytest.fixture
def barrier_fixture():
    llm = SimpleNamespace(
        _current_audio_response=None,
        send_client_event=AsyncMock(),
        _calculate_audio_duration_ms=lambda size: size // 48,
    )
    output = FakeOutput()
    barrier = NativeInterruptionBarrier(llm, output, timeout=0.05)
    yield barrier, llm, output
    barrier.close()


async def test_missing_output_ack_is_nonrecoverable_and_cannot_resume(barrier_fixture):
    barrier, llm, output = barrier_fixture
    source_start = AsyncMock()

    with pytest.raises(NativeInterruptionOrderingError) as caught:
        await barrier.speech_started(SimpleNamespace(), source_start)

    assert not isinstance(caught.value, (TimeoutError, OSError))
    source_start.assert_awaited_once()
    assert barrier.failed
    assert barrier.pending is None
    assert barrier.expected_frame_id is None
    llm.send_client_event.assert_not_awaited()

    # A late output callback cannot make an already failed receiver safe again.
    await output.processed(InterruptionFrame())
    with pytest.raises(NativeInterruptionOrderingError):
        await barrier.speech_started(SimpleNamespace(), source_start)
    source_start.assert_awaited_once()


async def test_only_bound_downstream_native_frame_can_acknowledge(barrier_fixture):
    barrier, llm, output = barrier_fixture
    barrier.timeout = 1
    source_seen = asyncio.Event()
    matching = InterruptionFrame()
    other = InterruptionFrame()

    async def source_start(event):
        barrier.observe_frame(other, FrameDirection.UPSTREAM)
        assert barrier.expected_frame_id is None
        barrier.observe_frame(matching, FrameDirection.DOWNSTREAM)
        source_seen.set()

    task = asyncio.create_task(barrier.speech_started(SimpleNamespace(), source_start))
    try:
        await asyncio.wait_for(source_seen.wait(), 0.5)
        assert barrier.expected_frame_id == matching.id
        await output.processed(other)
        assert not task.done()
        assert barrier.pending is not None and not barrier.pending.done()
        await output.processed(matching)
        await asyncio.wait_for(task, 0.5)
        assert not barrier.failed
        assert barrier.pending is None
        llm.send_client_event.assert_not_awaited()
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_cancelled_receiver_releases_pending_wait_and_output_handler(barrier_fixture):
    barrier, llm, output = barrier_fixture
    barrier.timeout = 1
    source_seen = asyncio.Event()

    async def source_start(event):
        source_seen.set()

    task = asyncio.create_task(barrier.speech_started(SimpleNamespace(), source_start))
    await asyncio.wait_for(source_seen.wait(), 0.5)
    pending = barrier.pending
    assert pending is not None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert pending.cancelled()
    assert barrier.pending is None
    assert barrier.expected_frame_id is None
    assert barrier.failed
    barrier.close()
    assert output.handlers == []
    llm.send_client_event.assert_not_awaited()


async def test_close_cancels_outstanding_ack_and_unregisters_handler(barrier_fixture):
    barrier, llm, output = barrier_fixture
    barrier.timeout = 1
    source_seen = asyncio.Event()

    async def source_start(event):
        source_seen.set()

    task = asyncio.create_task(barrier.speech_started(SimpleNamespace(), source_start))
    await asyncio.wait_for(source_seen.wait(), 0.5)
    pending = barrier.pending
    barrier.close()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 0.5)

    assert output.handlers == []
    assert pending is not None and pending.cancelled()
    assert barrier.pending is None
    llm.send_client_event.assert_not_awaited()


async def test_reset_requires_joined_receiver_then_clears_failure_and_duplicate_state(
    barrier_fixture,
):
    barrier, llm, output = barrier_fixture
    barrier.timeout = 1
    source_seen = asyncio.Event()

    async def source_start(event):
        source_seen.set()

    task = asyncio.create_task(barrier.speech_started(SimpleNamespace(), source_start))
    await asyncio.wait_for(source_seen.wait(), 0.5)
    pending = barrier.pending
    with pytest.raises(RuntimeError):
        barrier.reset_session()
    assert barrier.pending is pending
    assert barrier.remote_turn_open

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert barrier.failed
    assert barrier.remote_turn_open
    barrier.reset_session()
    assert not barrier.failed
    assert not barrier.remote_turn_open
    assert barrier.pending is None
    assert barrier.expected_frame_id is None

    async def fresh_source_start(event):
        frame = InterruptionFrame()
        barrier.observe_frame(frame, FrameDirection.DOWNSTREAM)
        await output.processed(frame)

    fresh_start = AsyncMock(side_effect=fresh_source_start)
    await barrier.speech_started(SimpleNamespace(), fresh_start)
    fresh_start.assert_awaited_once()
    assert barrier.remote_turn_open
    assert not barrier.failed
    llm.send_client_event.assert_not_awaited()


async def test_duplicate_start_does_not_wait_for_a_second_native_clear(barrier_fixture):
    barrier, llm, output = barrier_fixture

    async def source_start(event):
        frame = InterruptionFrame()
        barrier.observe_frame(frame, FrameDirection.DOWNSTREAM)
        await output.processed(frame)

    source_start = AsyncMock(side_effect=source_start)
    await barrier.speech_started(SimpleNamespace(), source_start)
    await barrier.speech_started(SimpleNamespace(), source_start)
    assert source_start.await_count == 1
    assert not barrier.failed

    async def source_stop(event):
        barrier.observe_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)

    await barrier.speech_stopped(SimpleNamespace(), source_stop)
    await barrier.speech_started(SimpleNamespace(), source_start)
    assert source_start.await_count == 2
    assert not barrier.failed
    llm.send_client_event.assert_not_awaited()


async def test_missing_native_stop_ack_cannot_silently_open_next_turn(barrier_fixture):
    barrier, llm, output = barrier_fixture
    barrier.remote_turn_open = True
    with pytest.raises(NativeInterruptionOrderingError):
        await barrier.speech_stopped(SimpleNamespace(), AsyncMock())
    assert barrier.failed and barrier.pending_stop is None
    with pytest.raises(NativeInterruptionOrderingError):
        await barrier.speech_started(SimpleNamespace(), AsyncMock())


async def test_service_unbind_cancels_old_wait_and_rebind_starts_fresh(service, brief):
    from dots_phone.pipecat_realtime import SupervisedRealtimeService

    cid = service.prepare("owner", brief)["call_id"]
    llm = SupervisedRealtimeService(supervisor=service, call_id=cid, api_key="offline-fixture")
    old_output = FakeOutput()
    llm.bind_output_transport(old_output)
    old_barrier = llm.interruption_barrier
    assert old_barrier is not None
    source_seen = asyncio.Event()

    async def source_start(event):
        source_seen.set()

    task = asyncio.create_task(old_barrier.speech_started(SimpleNamespace(), source_start))
    try:
        await asyncio.wait_for(source_seen.wait(), 0.5)
        llm.unbind_output_transport()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 0.5)
        assert old_output.handlers == []
        assert old_barrier.closed
        assert llm.interruption_barrier is None
        llm.unbind_output_transport()

        fresh_output = FakeOutput()
        llm.bind_output_transport(fresh_output)
        fresh = llm.interruption_barrier
        assert fresh is not None and fresh is not old_barrier
        assert not fresh.failed and not fresh.closed and not fresh.remote_turn_open

        async def fresh_source_start(event):
            frame = InterruptionFrame()
            fresh.observe_frame(frame, FrameDirection.DOWNSTREAM)
            # Detached output cannot acknowledge the new session's frame.
            await old_output.processed(frame)
            assert fresh.pending is not None and not fresh.pending.done()
            await fresh_output.processed(frame)

        await fresh.speech_started(SimpleNamespace(), fresh_source_start)
        assert not fresh.failed
    finally:
        llm.unbind_output_transport()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert service.provider.dials == []


async def test_missing_ack_terminates_voice_path_without_model_reconnect(service, brief):
    from dots_phone.pipecat_realtime import SupervisedRealtimeService
    from tests.conftest import started

    # started uses the fixture's FakeProvider; socket connections are forbidden.
    cid, _ = await started(service, brief)
    llm = SupervisedRealtimeService(supervisor=service, call_id=cid, api_key="offline-fixture")
    llm._connect = AsyncMock()
    output = FakeOutput()
    llm.bind_output_transport(output)
    barrier = llm.interruption_barrier
    assert barrier is not None
    barrier.timeout = 0.01
    try:
        with pytest.raises(NativeInterruptionOrderingError) as caught:
            await barrier.speech_started(SimpleNamespace(), AsyncMock())
        llm.voice_failure("receive_exception", caught.value)
        assert llm.reconnect_task is None
        assert llm.reconnect_attempts == 0
        assert llm.stop_task is not None
        await llm.stop_task
        call = service.call("owner", cid)
        assert call["state"] == "failed"
        assert call["usage"]["realtime_path_failure"] == {
            "phase": "receive_exception",
            "exception_type": "NativeInterruptionOrderingError",
        }
        llm._connect.assert_not_awaited()
        assert len(service.provider.dials) == len(service.provider.stops) == 1
    finally:
        llm.unbind_output_transport()
