"""No-network observations must leave carrier transport semantics unchanged."""

import asyncio
import json
import socket

import pytest
from starlette.websockets import WebSocketDisconnect, WebSocketState

from dots_phone.carrier_lifecycle import CarrierLifecycleSocket
from dots_phone.carrier_stream import CarrierStream


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Carrier lifecycle tests must not connect or resolve DNS")

    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


class Socket:
    def __init__(self):
        self.headers = {"private-header": "private-value"}
        self.application_state = self.client_state = WebSocketState.CONNECTED
        self.messages = asyncio.Queue()
        self.sent = []
        self.closed = []

    async def receive(self):
        item = await self.messages.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def send_text(self, text):
        self.sent.append(text)

    async def close(self, code=1000, reason=None):
        self.closed.append((code, reason))
        self.application_state = self.client_state = WebSocketState.DISCONNECTED


def observe(socket, **kwargs):
    wrapper = CarrierLifecycleSocket(socket, **kwargs)
    events = []
    wrapper.bind(lambda kind, **fields: events.append({"kind": kind, **fields}))
    return wrapper, events


async def test_raw_stop_and_disconnect_are_distinct_and_content_free():
    source = Socket()
    wrapper, events = observe(source)
    messages = [
        {"type": "websocket.receive", "text": '{"event":"media","payload":"private-audio"}'},
        {"type": "websocket.receive", "text": "not JSON private-content"},
        {"type": "websocket.receive", "text": "[1, 2]"},
        {
            "type": "websocket.receive",
            "text": '{"event":"stop","callSid":"private-id","reason":"private-reason"}',
        },
        {"type": "websocket.disconnect", "code": 1000, "reason": "private-reason"},
    ]
    for message in messages:
        source.messages.put_nowait(message)
        assert await wrapper.receive() is message
    assert events == [
        {"kind": "carrier_stop", "code": None},
        {"kind": "carrier_disconnect", "code": 1000},
    ]
    assert "private" not in json.dumps(events)
    assert source.closed == []  # Observing stop never closes or manufactures input.
    assert wrapper.headers is source.headers
    await wrapper.send_text("unchanged outgoing contents")
    assert source.sent == ["unchanged outgoing contents"]


async def test_disconnect_exception_is_observed_without_swallowing_or_retaining_reason():
    source = Socket()
    wrapper, events = observe(source)
    error = WebSocketDisconnect(1006, "private-reason")
    source.messages.put_nowait(error)
    with pytest.raises(WebSocketDisconnect) as raised:
        await wrapper.receive()
    assert raised.value is error
    assert events == [{"kind": "carrier_disconnect", "code": 1006}]


@pytest.mark.parametrize("code", [True, "private-code", 4001, 999, None])
async def test_unrecognized_close_code_is_not_retained(code):
    source = Socket()
    wrapper, events = observe(source)
    message = {"type": "websocket.disconnect", "code": code}
    source.messages.put_nowait(message)
    assert await wrapper.receive() is message
    assert events == [{"kind": "carrier_disconnect", "code": None}]


async def test_local_close_is_a_request_and_original_arguments_still_pass_through():
    source = Socket()
    wrapper, events = observe(source)
    await wrapper.close(code=1001, reason="private-close-reason")
    assert source.closed == [(1001, "private-close-reason")]
    assert events == [{"kind": "carrier_close_requested", "code": 1001}]


async def test_observation_failure_cannot_change_socket_behavior():
    source = Socket()
    wrapper = CarrierLifecycleSocket(source)

    def failed_observation(*args, **kwargs):
        raise ValueError("private diagnostic failure")

    wrapper.bind(failed_observation)
    message = {"type": "websocket.disconnect", "code": 1000}
    source.messages.put_nowait(message)
    assert await wrapper.receive() is message
    await wrapper.close()
    assert source.closed == [(1000, None)]


async def test_intentional_keypad_replacement_is_not_a_logical_disconnect():
    first, second = Socket(), Socket()
    stream = CarrierStream(first, "fictional-first-stream")
    wrapper, events = observe(stream, logical=True)
    try:
        nonce = stream.begin_change()
        first.messages.put_nowait({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(stream.active.detached.wait(), 1)
        assert not stream.closed
        await stream.attach(second, "fictional-second-stream", nonce)
        media = {"type": "websocket.receive", "text": '{"event":"media","payload":"same"}'}
        second.messages.put_nowait(media)
        assert await asyncio.wait_for(wrapper.receive(), 1) is media
        assert events == []

        # A later actual lease loss ends the logical transport, but the generated
        # 1000 code is not a remotely observed carrier close code.
        second.messages.put_nowait({"type": "websocket.disconnect", "code": 1006})
        assert (await asyncio.wait_for(wrapper.receive(), 1))["type"] == "websocket.disconnect"
        assert events == [{"kind": "carrier_transport_disconnect", "code": None}]
        await wrapper.close()
        assert events[-1] == {"kind": "carrier_transport_close_requested", "code": None}
    finally:
        await stream.close()
