"""Observe carrier boundaries without changing or retaining received contents."""

from __future__ import annotations

import json

from starlette.websockets import WebSocketDisconnect

_CODES = {1000, 1001, 1002, 1003, 1006, 1007, 1008, 1009, 1010, 1011, 1012, 1013, 1014, 1015}


class CarrierLifecycleSocket:
    """Transparent post-authentication socket proxy with an in-memory observer.

    A keypad CarrierStream is a logical transport: its disconnect can be
    synthesized and must never be represented as a remote close frame. Wrapping
    that transport also keeps intentional stream replacement out of call-end
    metadata. Neither socket messages nor close-reason text are retained.
    """

    def __init__(self, socket, *, logical=False):
        self.socket = socket
        self.logical = logical
        self.observer = None

    def __getattr__(self, name):
        return getattr(self.socket, name)

    def bind(self, observer):
        self.observer = observer

    def _observe(self, kind, code=None):
        if self.observer is None:
            return
        prefix = "carrier_transport_" if self.logical else "carrier_"
        safe_code = code if not self.logical and type(code) is int and code in _CODES else None
        try:
            self.observer(prefix + kind, code=safe_code)
        except Exception:
            # Optional metadata cannot change the socket's forwarding or teardown.
            pass

    async def receive(self):
        try:
            message = await self.socket.receive()
        except WebSocketDisconnect as error:
            self._observe("disconnect", error.code)
            raise
        if message.get("type") == "websocket.disconnect":
            self._observe("disconnect", message.get("code"))
        elif not self.logical:
            text = message.get("text")
            if isinstance(text, str) and len(text) <= 16384:
                try:
                    event = json.loads(text)
                except (ValueError, RecursionError):
                    event = None
                if isinstance(event, dict) and event.get("event") == "stop":
                    self._observe("stop")
        return message

    async def close(self, code=1000, reason=None):
        self._observe("close_requested", code)
        if reason is None:
            return await self.socket.close(code=code)
        return await self.socket.close(code=code, reason=reason)
