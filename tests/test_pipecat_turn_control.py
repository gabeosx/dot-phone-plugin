import asyncio

import pytest

from dots_phone.pipecat_turn_control import ManualTurnControlError, ManualTurnController


class Harness:
    def __init__(self, **kwargs):
        self.wire = []
        self.failures = []
        self.append_entered = asyncio.Event()
        self.append_release = asyncio.Event()
        self.append_release.set()
        self.commit_entered = asyncio.Event()
        self.ack_commits = True
        self.control = ManualTurnController(
            append=self.append,
            flush=self.flush,
            interrupt=self.interrupt,
            commit=self.commit,
            create=self.create,
            fail=self.fail,
            **kwargs,
        )

    async def append(self, frame):
        self.wire.append(("append", frame))
        self.append_entered.set()
        await self.append_release.wait()

    async def flush(self):
        self.wire.append(("flush",))

    async def interrupt(self, turn, cleared_at):
        self.wire.append(("interrupt", turn, cleared_at))

    async def commit(self):
        self.wire.append(("commit",))
        self.commit_entered.set()
        if self.ack_commits:
            self.control.input_committed()

    async def create(self, turn):
        self.control.response_requested(turn)
        self.wire.append(("create", turn))

    async def fail(self, error):
        self.failures.append(error)

    async def drain(self):
        await asyncio.wait_for(self.control.wait_idle(), 0.5)


@pytest.mark.asyncio
async def test_audio_and_turn_commands_are_fifo_without_input_clear_or_replay():
    h = Harness()
    c = h.control
    try:
        h.append_release.clear()
        c.enqueue_audio(b"onset")
        await h.append_entered.wait()
        c.begin_turn(101)
        c.output_cleared(101, 900)
        c.enqueue_audio(b"body")
        c.end_turn()
        c.enqueue_audio(b"next-buffer")
        assert h.wire == [("append", b"onset")]
        h.append_release.set()
        await h.drain()
        assert h.wire == [
            ("append", b"onset"),
            ("interrupt", 1, 900),
            ("append", b"body"),
            ("flush",),
            ("commit",),
            ("create", 1),
            ("append", b"next-buffer"),
        ]
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_exact_clear_ack_required_without_blocking_ingress():
    h = Harness()
    c = h.control
    try:
        c.begin_turn(42)
        c.output_cleared(41, 100)
        c.enqueue_audio(b"speech")
        c.end_turn()
        await asyncio.sleep(0)
        assert h.wire == []
        assert c.pending_commands == 2
        with pytest.raises(ManualTurnControlError):
            c.response_requested(1)
        c.output_cleared(42, 101)
        await h.drain()
        assert h.wire[0] == ("interrupt", 1, 101)
        assert h.wire[-1] == ("create", 1)
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_completed_generation_remains_playable_until_next_turn():
    h = Harness()
    c = h.control
    try:
        c.response_requested(0)
        c.response_created("initial")
        c.response_done("initial")
        assert c.allows_audio("initial")
        c.begin_turn(1)
        assert not c.allows_audio("initial")
        c.output_cleared(1, 10)
        c.end_turn()
        await h.drain()
        c.response_created("reply")
        c.response_done("reply")
        assert c.allows_audio("reply")
        assert not c.allows_audio("initial")
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_onset_revokes_response_before_turn_projection_without_advancing_identity():
    h = Harness()
    c = h.control
    try:
        c.response_requested(0)
        c.response_created("queued")
        c.response_done("queued")
        assert c.allows_audio("queued")
        assert not c.revoke_response(1)
        assert c.revoke_response(0)
        assert c.turn_id == 0 and not c.speaking
        assert not c.can_respond(0) and not c.allows_audio("queued")
        with pytest.raises(ManualTurnControlError):
            c.response_requested(0)
        assert c.begin_turn(1) == 1
        c.output_cleared(1, 10)
        c.end_turn()
        await h.drain()
        assert h.wire[-1] == ("create", 1)
        c.response_created("new")
        assert c.allows_audio("new") and not c.allows_audio("queued")
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_new_speech_during_commit_ack_suppresses_old_response_request():
    h = Harness()
    c = h.control
    try:
        h.ack_commits = False
        c.begin_turn(1)
        c.output_cleared(1, 10)
        c.enqueue_audio(b"first")
        c.end_turn()
        await h.commit_entered.wait()
        assert not c.can_respond(1)
        with pytest.raises(ManualTurnControlError):
            c.response_requested(1)
        c.begin_turn(2)
        c.output_cleared(2, 20)
        c.enqueue_audio(b"second")
        c.input_committed()
        await h.drain()
        assert not any(event[0] == "create" for event in h.wire)
        assert h.wire[-2:] == [("interrupt", 2, 20), ("append", b"second")]
        h.ack_commits = True
        c.end_turn()
        await h.drain()
        assert [event for event in h.wire if event[0] == "create"] == [("create", 2)]
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_cancel_completion_is_required_before_new_response():
    h = Harness()
    c = h.control
    try:
        c.response_requested(0)
        c.response_created("old")
        c.begin_turn(1)
        c.output_cleared(1, 10)
        c.end_turn()
        await h.commit_entered.wait()
        await asyncio.sleep(0)
        assert not any(event[0] == "create" for event in h.wire)
        c.response_done("old")
        await h.drain()
        assert h.wire[-1] == ("create", 1)
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_new_speech_wakes_obsolete_wait_for_cancel_completion():
    h = Harness()
    c = h.control
    try:
        c.response_requested(0)
        c.response_created("old")
        c.begin_turn(1)
        c.output_cleared(1, 10)
        c.end_turn()
        await h.commit_entered.wait()
        await asyncio.sleep(0)
        c.begin_turn(2)
        c.output_cleared(2, 20)
        await h.drain()
        assert h.wire[-1] == ("interrupt", 2, 20)
        assert not any(event[0] == "create" for event in h.wire)
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_duplicate_boundaries_do_not_duplicate_commit_or_create():
    h = Harness()
    c = h.control
    try:
        assert c.begin_turn(1) == c.begin_turn(2) == 1
        c.output_cleared(1, 10)
        c.end_turn()
        c.end_turn()
        await h.drain()
        assert c.commits == c.response_requests == 1
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_clear_timeout_fails_closed_and_drains_pending_work():
    h = Harness(timeout=0.01)
    c = h.control
    try:
        c.begin_turn(1)
        c.enqueue_audio(b"speech")
        c.end_turn()
        await h.drain()
        await asyncio.sleep(0)
        assert isinstance(c.failure, TimeoutError)
        assert len(h.failures) == 1
        assert h.wire == []
        assert not c.allows_audio("anything")
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_close_joins_old_work_and_late_ack_cannot_release_replacement():
    old, new = Harness(), Harness()
    try:
        old.control.begin_turn(1)
        await asyncio.sleep(0)
        await old.control.close()
        new.control.begin_turn(2)
        new.control.end_turn()
        old.control.output_cleared(1, 10)
        old.control.input_committed()
        await asyncio.sleep(0)
        assert old.wire == new.wire == []
        new.control.output_cleared(2, 20)
        await new.drain()
        assert new.wire[-1] == ("create", 1)
    finally:
        await old.control.close()
        await new.control.close()


@pytest.mark.asyncio
async def test_unrequested_response_fails_closed():
    h = Harness()
    try:
        h.control.response_created("unrequested")
        await asyncio.sleep(0)
        assert isinstance(h.control.failure, ManualTurnControlError)
        assert not h.control.allows_audio("unrequested")
    finally:
        await h.control.close()


@pytest.mark.asyncio
async def test_never_ready_fails_closed_without_any_provider_send():
    h = Harness(ready=asyncio.Event(), ready_timeout=0.01)
    try:
        h.control.enqueue_audio(b"speech")
        await h.drain()
        await asyncio.sleep(0)
        assert isinstance(h.control.failure, TimeoutError)
        assert h.wire == []
    finally:
        await h.control.close()


@pytest.mark.asyncio
async def test_create_sends_serialize_and_superseded_waiter_is_skipped():
    h = Harness()
    entered, release = asyncio.Event(), asyncio.Event()
    wire = []

    async def first():
        entered.set()
        await release.wait()
        wire.append("first")

    async def second():
        wire.append("second")

    try:
        first_task = asyncio.create_task(h.control.send_response(0, first))
        await entered.wait()
        second_task = asyncio.create_task(h.control.send_response(0, second))
        await asyncio.sleep(0)
        h.control.begin_turn(1)
        release.set()
        assert await first_task is True
        assert await second_task is False
        assert wire == ["first"]
        h.control.response_created("old")
        assert not h.control.allows_audio("old")
    finally:
        await h.control.close()


async def test_proposal_revokes_now_but_only_exact_aggregator_clear_releases_fifo():
    h = Harness()
    c = h.control
    try:
        c.response_requested(0)
        c.response_created("old")
        c.response_done("old")
        assert c.allows_audio("old")
        c.begin_turn()  # Source proposal precedes actual aggregator frame ID.
        assert not c.allows_audio("old")
        c.enqueue_audio(b"raw-next-input")
        c.output_cleared(41, 100)
        await asyncio.sleep(0)
        assert h.wire == []
        c.begin_turn(42)
        c.output_cleared(41, 101)
        await asyncio.sleep(0)
        assert h.wire == []
        c.output_cleared(42, 102)
        await h.drain()
        assert h.wire == [("interrupt", 1, 102), ("append", b"raw-next-input")]
    finally:
        await c.close()


async def test_same_turn_continuations_coalesce_while_create_pending_or_active():
    h = Harness()
    sends = []

    async def send():
        sends.append("response.create")

    try:
        assert await h.control.send_response(0, send)
        assert not await h.control.send_response(0, send)  # Pending response.created.
        h.control.response_created("first")
        assert not await h.control.send_response(0, send)  # Still generating.
        h.control.response_done("first")
        assert await h.control.send_response(0, send)
        assert sends == ["response.create", "response.create"]
    finally:
        await h.control.close()
