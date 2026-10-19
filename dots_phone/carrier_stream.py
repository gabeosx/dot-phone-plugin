"""Bounded, intentional carrier stream replacement without restarting voice reasoning.

Only a pending, single-use nonce can attach a new authenticated carrier socket.
Unexpected network disconnects still terminate the pipeline; this is not redial
or a general reconnect/replay policy.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from dataclasses import dataclass

from starlette.websockets import WebSocketDisconnect, WebSocketState


@dataclass
class StreamLease:
    websocket: object
    stream_sid: str
    detached: asyncio.Event
    reader: asyncio.Task | None = None


class CarrierStream:
    def __init__(self, websocket, stream_sid):
        self.headers = websocket.headers
        self.incoming = asyncio.Queue(maxsize=250)
        self.active = None
        self.pending_nonce = None
        self.reattached = asyncio.Event()
        self.closed = False
        self.stream_count = 0
        self.last_media_received_at = None
        self.on_rebind = None
        self._install(websocket, stream_sid)

    @property
    def application_state(self):
        return WebSocketState.DISCONNECTED if self.closed else WebSocketState.CONNECTED

    @property
    def client_state(self):
        return self.application_state

    def _install(self, websocket, stream_sid):
        lease = StreamLease(websocket, stream_sid, asyncio.Event())
        self.active = lease
        self.stream_count += 1
        lease.reader = asyncio.create_task(self._read(lease), name="carrier-stream-reader")
        return lease

    async def _read(self, lease):
        try:
            while not self.closed and self.active is lease:
                message = await lease.websocket.receive()
                if message["type"] == "websocket.disconnect":
                    break
                if message.get("text"):
                    event = json.loads(message["text"]).get("event")
                    if event == "stop":
                        break
                    if event == "media":
                        self.last_media_received_at = time.monotonic()
                if self.active is lease:
                    self.incoming.put_nowait(message)
        except (WebSocketDisconnect, RuntimeError, ValueError, asyncio.QueueFull):
            pass
        finally:
            lease.detached.set()
            if self.active is lease and not self.pending_nonce:
                self._disconnect()

    def _disconnect(self):
        self.closed = True
        self.pending_nonce = None
        self.reattached.set()
        while not self.incoming.empty():
            self.incoming.get_nowait()
        self.incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})

    async def receive(self):
        return await self.incoming.get()

    def begin_change(self):
        if self.closed or self.pending_nonce:
            raise RuntimeError("carrier stream unavailable or change already pending")
        self.pending_nonce = secrets.token_hex(32)
        self.reattached.clear()
        return self.pending_nonce

    async def attach(self, websocket, stream_sid, nonce):
        if (
            self.closed
            or not self.pending_nonce
            or not isinstance(nonce, str)
            or not secrets.compare_digest(nonce, self.pending_nonce)
            or stream_sid == self.active.stream_sid
        ):
            raise ValueError("unrequested or stale carrier stream replacement")
        old = self.active
        while not self.incoming.empty():
            self.incoming.get_nowait()
        if self.on_rebind:
            self.on_rebind(stream_sid)
        lease = self._install(websocket, stream_sid)
        self.pending_nonce = None
        self.reattached.set()
        # Closing an old ASGI socket cannot delay the live replacement. Its
        # reader owns cleanup and cannot close or feed the newly active lease.
        if old.reader and not old.reader.done():
            old.reader.cancel()
        await self._close_socket(old.websocket)
        return lease

    async def wait_change(self, timeout=12):
        try:
            await asyncio.wait_for(self.reattached.wait(), timeout)
            if self.closed or self.pending_nonce:
                raise RuntimeError("carrier replacement unavailable")
        except (TimeoutError, RuntimeError):
            await self.close()
            raise

    async def send_text(self, text):
        if self.closed or self.pending_nonce:
            return  # Never accumulate/replay speech across keypad control.
        payload = json.loads(text)
        if "streamSid" in payload:
            payload["streamSid"] = self.active.stream_sid
        await self.active.websocket.send_text(json.dumps(payload))

    async def send_bytes(self, data):
        if not self.closed and not self.pending_nonce:
            await self.active.websocket.send_bytes(data)

    async def _close_socket(self, websocket, code=1000):
        if (
            websocket.application_state == WebSocketState.CONNECTED
            and websocket.client_state == WebSocketState.CONNECTED
        ):
            try:
                await asyncio.wait_for(websocket.close(code=code), timeout=0.5)
            except (TimeoutError, WebSocketDisconnect, RuntimeError):
                pass

    async def close(self, code=1000):
        lease = self.active
        self._disconnect()
        if lease.reader and not lease.reader.done():
            lease.reader.cancel()
            await asyncio.gather(lease.reader, return_exceptions=True)
        await self._close_socket(lease.websocket, code)


NUMBERS = dict(
    zip("zero one two three four five six seven eight nine".split(), "0123456789", strict=True)
)


def normalize_menu(text):
    import re

    return " ".join(NUMBERS.get(word, word) for word in re.findall(r"[a-z0-9]+", text.lower()))


def keypad_menu_selection(digit, observed, *, recipient_input=False):
    """Bound the control, leaving the current menu's meaning to the call agents.

    A transcript proves that recipient context exists, not which option is right.
    The active call controller separately enforces the owner's permitted actions.
    """
    import re

    return (
        isinstance(digit, str)
        and re.fullmatch(r"[0-9*#]", digit) is not None
        and isinstance(observed, str)
        and (bool(observed.strip()) or recipient_input is True)
    )


def reservation_menu_option(digit, quote, observed, *, recipient_input=False):
    """Compatibility entry point; model-written quotes do not authorize controls."""
    return keypad_menu_selection(digit, observed, recipient_input=recipient_input)
