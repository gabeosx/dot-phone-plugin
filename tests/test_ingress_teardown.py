"""No-network gateway lifecycle regressions using both real forwarding loops."""

import asyncio
import json
import logging
import socket
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import pytest
from starlette.websockets import WebSocketDisconnect

from dots_phone import ingress
from dots_phone.ingress import create_gateway
from dots_phone.ingress_diagnostics import IngressDiagnostics, close_code

SECRET = "synthetic-secret-call-id-phone-header-close-reason-message"
LOGGER = "uvicorn.error.dots_phone.ingress"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    assert Path(ingress.__file__).resolve().parent.parent == Path(__file__).resolve().parent.parent
    attempts = []

    def denied(*args, **kwargs):
        attempts.append(True)
        raise AssertionError("Gateway teardown regression attempted networking")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    yield
    assert attempts == []


class Carrier:
    def __init__(self, *, close_error=None, send_error=None):
        self.headers = {"x-twilio-signature": SECRET, "other-private-header": SECRET}
        self.url = SimpleNamespace(query="")
        self.incoming = asyncio.Queue()
        self.sent = []
        self.close_calls = []
        self.close_error = close_error
        self.send_error = send_error
        self.accepted = False

    async def accept(self):
        self.accepted = True

    async def receive_text(self):
        item = await self.incoming.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def send_text(self, text):
        if self.send_error:
            raise self.send_error
        self.sent.append(text)

    async def close(self, code=1000):
        self.close_calls.append(code)
        if self.close_error:
            raise self.close_error


class Upstream:
    def __init__(self, *, close_code=1000, send_error=None, enter_error=None):
        self.close_code = close_code
        self.send_error = send_error
        self.enter_error = enter_error
        self.incoming = asyncio.Queue()
        self.sent = []
        self.entered = asyncio.Event()
        self.exited = False

    async def __aenter__(self):
        self.entered.set()
        if self.enter_error:
            raise self.enter_error
        return self

    async def __aexit__(self, *args):
        self.exited = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.incoming.get()
        if item is None:
            raise StopAsyncIteration
        if isinstance(item, BaseException):
            raise item
        return item

    async def send_str(self, text):
        if self.send_error:
            raise self.send_error
        self.sent.append(text)


@pytest.fixture
def gateway(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    sessions = []

    def prepare(carrier, upstream):
        class Session:
            async def __aenter__(self):
                sessions.append(self)
                self.connection_args = None
                return self

            async def __aexit__(self, *args):
                pass

            def ws_connect(self, *args, **kwargs):
                self.connection_args = (args, kwargs)
                return upstream

        monkeypatch.setattr(aiohttp, "ClientSession", Session)
        route = next(route for route in create_gateway("media").routes if route.path == "/voice")

        async def run():
            await asyncio.wait_for(route.endpoint(carrier), 1)

        return run

    def report():
        records = [row for row in caplog.records if row.name == LOGGER]
        assert len(records) == 1
        assert records[0].exc_info is None
        text = records[0].getMessage()
        assert SECRET not in text
        assert len(text.encode()) < 2000
        value = json.loads(text)
        assert set(value) == {
            "event",
            "schema_version",
            "first_observed_boundary",
            "completion_observation",
            "inferred_initiator",
            "elapsed_ms",
            "incoming",
            "outgoing",
            "forwarded_to_upstream_messages",
            "forwarded_to_upstream_bytes",
            "forwarded_to_carrier_messages",
            "forwarded_to_carrier_bytes",
            "cleanup_close_outcome",
            "cleanup_close_code",
        }
        assert value["event"] == "media_gateway_teardown"
        assert value["schema_version"] == 1
        assert value["inferred_initiator"] is False
        for direction in ("incoming", "outgoing"):
            if value[direction]:
                assert set(value[direction]) == {"boundary", "outcome", "close_code", "at_ms"}
                assert 0 <= value[direction]["at_ms"] <= value["elapsed_ms"]
        return value

    return SimpleNamespace(prepare=prepare, report=report, sessions=sessions)


@pytest.mark.asyncio
async def test_carrier_normal_hangup_keeps_first_edge_and_forwarding(gateway):
    # Real incident regression: cleanup attempts a close after peer departure,
    # producing a new local1006. Preserve the initial received1000 separately.
    carrier = Carrier(close_error=WebSocketDisconnect(1006, SECRET))
    upstream = Upstream()
    inbound = json.dumps({"event": "media", "payload": SECRET})
    outbound = json.dumps({"event": "clear", "streamSid": SECRET})
    carrier.incoming.put_nowait(inbound)
    carrier.incoming.put_nowait(WebSocketDisconnect(1000, SECRET))
    upstream.incoming.put_nowait(SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=outbound))
    await gateway.prepare(carrier, upstream)()
    assert upstream.sent == [inbound] and carrier.sent == [outbound]
    assert carrier.accepted and upstream.exited
    assert gateway.sessions[0].connection_args[1]["headers"] == {"x-twilio-signature": SECRET}
    record = gateway.report()
    assert record["first_observed_boundary"] == "carrier"
    assert record["completion_observation"] == "one_direction_ready"
    assert record["incoming"]["outcome"] == "peer_disconnected"
    assert record["incoming"]["close_code"] == 1000
    assert record["outgoing"]["outcome"] == "cancelled"
    assert record["cleanup_close_outcome"] == "already_disconnected"
    assert record["cleanup_close_code"] == 1006
    assert record["forwarded_to_upstream_messages"] == record["forwarded_to_carrier_messages"] == 1
    assert record["forwarded_to_upstream_bytes"] == len(inbound.encode())
    assert record["forwarded_to_carrier_bytes"] == len(outbound.encode())


@pytest.mark.asyncio
async def test_premature_carrier_disconnect_is_observed_without_claiming_initiator(gateway):
    carrier, upstream = Carrier(), Upstream()
    carrier.incoming.put_nowait(WebSocketDisconnect(1006, SECRET))
    await gateway.prepare(carrier, upstream)()
    record = gateway.report()
    assert record["first_observed_boundary"] == "carrier"
    assert record["incoming"]["close_code"] == 1006
    assert record["forwarded_to_upstream_messages"] == 0
    assert record["cleanup_close_outcome"] == "completed"


@pytest.mark.asyncio
async def test_application_upstream_end_is_distinct_from_carrier_or_model_cause(gateway):
    carrier, upstream = Carrier(), Upstream(close_code=1001)
    upstream.incoming.put_nowait(None)
    await gateway.prepare(carrier, upstream)()
    record = gateway.report()
    assert record["first_observed_boundary"] == "upstream"
    assert record["outgoing"]["outcome"] == "stream_ended"
    assert record["outgoing"]["close_code"] == 1001
    assert record["incoming"]["outcome"] == "cancelled"
    assert "model" not in json.dumps(record)


@pytest.mark.asyncio
async def test_both_ready_before_wait_resumes_is_ambiguous_not_physical_order(gateway):
    carrier, upstream = Carrier(), Upstream()
    carrier.incoming.put_nowait(WebSocketDisconnect(1000, SECRET))
    upstream.incoming.put_nowait(None)
    await gateway.prepare(carrier, upstream)()
    record = gateway.report()
    assert record["first_observed_boundary"] == "ambiguous"
    assert record["completion_observation"] == "both_directions_ready"
    assert record["incoming"]["boundary"] == "carrier"
    assert record["outgoing"]["boundary"] == "upstream"


@pytest.mark.parametrize("boundary", ["carrier", "upstream"])
@pytest.mark.asyncio
async def test_non_close_forwarding_error_propagates_and_private_text_is_not_logged(
    gateway, boundary
):
    failure = ValueError(SECRET)
    carrier = Carrier(
        close_error=WebSocketDisconnect(1006, SECRET),
        send_error=failure if boundary == "carrier" else None,
    )
    upstream = Upstream(send_error=failure if boundary == "upstream" else None)
    if boundary == "carrier":
        upstream.incoming.put_nowait(SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=SECRET))
    else:
        carrier.incoming.put_nowait(SECRET)
    with pytest.raises(ValueError) as caught:
        await gateway.prepare(carrier, upstream)()
    assert caught.value is failure
    record = gateway.report()
    direction = "outgoing" if boundary == "carrier" else "incoming"
    assert record["first_observed_boundary"] == boundary
    assert record[direction]["outcome"] == "send_failed"
    assert record["cleanup_close_outcome"] == "already_disconnected"
    assert record["forwarded_to_upstream_messages"] == record["forwarded_to_carrier_messages"] == 0


@pytest.mark.parametrize("peer_already_failed", [False, True])
@pytest.mark.asyncio
async def test_both_tasks_are_drained_and_original_error_priority_is_preserved(
    gateway,
    monkeypatch,
    peer_already_failed,
):
    first, second = ValueError(SECRET), RuntimeError(SECRET)
    cancelled_peer_finished = asyncio.Event()

    class CancellationFailure(Upstream):
        async def __anext__(self):
            try:
                return await super().__anext__()
            except asyncio.CancelledError:
                cancelled_peer_finished.set()
                raise second from None

    carrier, upstream = Carrier(), CancellationFailure()
    carrier.incoming.put_nowait(first)
    if peer_already_failed:
        upstream.incoming.put_nowait(second)
    created = []
    original_create_task = asyncio.create_task

    def tracked(coroutine, *args, **kwargs):
        task = original_create_task(coroutine, *args, **kwargs)
        created.append(task)
        return task

    monkeypatch.setattr(asyncio, "create_task", tracked)
    with pytest.raises(ValueError) as caught:
        await gateway.prepare(carrier, upstream)()
    assert caught.value is first
    assert len(created) == 2 and all(task.done() for task in created)
    assert cancelled_peer_finished.is_set() is (not peer_already_failed)
    # Production cleanup must retrieve secondary failures as well as preserve
    # the original primary error; the test does not consume them on its behalf.
    assert all(task._log_traceback is False for task in created)
    record = gateway.report()
    assert record["incoming"]["outcome"] == record["outgoing"]["outcome"] == "receive_failed"
    assert record["completion_observation"] == (
        "both_directions_ready" if peer_already_failed else "one_direction_ready"
    )


@pytest.mark.asyncio
async def test_upstream_connect_failure_keeps_existing_handled_behavior(gateway):
    carrier = Carrier()
    upstream = Upstream(enter_error=aiohttp.ClientConnectionError(SECRET))
    await gateway.prepare(carrier, upstream)()
    record = gateway.report()
    assert record["first_observed_boundary"] == "upstream"
    assert record["completion_observation"] == "upstream_setup_failed"
    assert record["incoming"] is None and record["outgoing"] is None


@pytest.mark.asyncio
async def test_message_size_limit_preserves_policy_and_never_logs_message(gateway):
    carrier, upstream = Carrier(), Upstream()
    carrier.incoming.put_nowait(SECRET * 1000)
    await gateway.prepare(carrier, upstream)()
    record = gateway.report()
    assert upstream.sent == [] and carrier.close_calls == [1009, 1000]
    assert record["first_observed_boundary"] == "gateway"
    assert record["incoming"]["outcome"] == "message_size_limit"
    assert record["incoming"]["close_code"] == 1009


@pytest.mark.asyncio
async def test_cancellation_does_not_invent_a_remote_close(gateway):
    carrier, upstream = Carrier(), Upstream()
    task = asyncio.create_task(gateway.prepare(carrier, upstream)())
    await asyncio.wait_for(upstream.entered.wait(), 1)
    await asyncio.sleep(0)  # Allow both forwarding tasks to enter queue waits.
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    record = gateway.report()
    assert record["first_observed_boundary"] == "unknown"
    assert record["completion_observation"] == "not_observed"


@pytest.mark.parametrize(
    "value,expected",
    [
        (1000, 1000),
        (1006, 1006),
        (1011, 1011),
        (aiohttp.WSCloseCode.OK, 1000),
        (4001, None),
        (SECRET, None),
        ("1000", None),
        (True, None),
        (None, None),
    ],
)
def test_only_standard_numeric_close_codes_are_retained(value, expected):
    assert close_code(value) == expected


def test_diagnostic_numbers_are_bounded_and_first_observation_is_immutable():
    clock = [0]
    record = IngressDiagnostics(clock=lambda: clock[0])
    clock[0] = 999999999
    record.forwarded_message("incoming", 999999999999)
    record.end("incoming", "carrier", "peer_disconnected", 1000)
    record.end("incoming", "unknown", "cancelled", 1006)
    record.completed(["incoming"])
    value = record.report()
    assert value["elapsed_ms"] == value["incoming"]["at_ms"] == 86400000
    assert value["forwarded_to_upstream_bytes"] == 1000000000
    assert value["incoming"]["close_code"] == 1000
