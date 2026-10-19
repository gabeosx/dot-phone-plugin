"""Scripted local provider. Tone markers are protocol fixtures, never model evidence."""

import asyncio
import base64
import contextlib
import json

import numpy as np


class FakeLive:
    def __init__(
        self,
        *,
        omit_closed=False,
        omit_usage=False,
        start_delay=0,
        end_completion_delay=0,
        close_delay=0,
    ):
        self.omit_closed = omit_closed
        self.omit_usage = omit_usage
        self.start_delay = start_delay
        self.end_completion_delay = end_completion_delay
        self.close_delay = close_delay
        self.received = []
        self.stage = 0
        self.speaker = None
        self.interrupted = False
        self.close_requested = False
        self.post_close_events = []
        self.background = []
        self.continuations = 0

    async def handle(self, ws):
        async def send(data):
            await ws.send(json.dumps(data))

        async def caption(role, text):
            kind = "input" if role == "user" else "output"
            await send(
                {
                    "type": f"session.{kind}_transcript.delta",
                    "delta": text,
                    "start_ms": 0,
                    "end_ms": 100,
                }
            )

        async def speak(text, ticks):
            await caption("assistant", text)
            pcm = np.full(480, 4000, dtype="<i2").tobytes()
            for _ in range(ticks):
                await send(
                    {"type": "session.output_audio.delta", "delta": base64.b64encode(pcm).decode()}
                )
                await asyncio.sleep(0.02)

        async def call(name, args):
            call_id = "call_" + name
            await send(
                {
                    "type": "response.event",
                    "delegation_id": call_id,
                    "event": {"type": "response.created", "response": {"output": []}},
                }
            )
            await send(
                {
                    "type": "response.event",
                    "delegation_id": call_id,
                    "event": {
                        "type": "response.output_item.done",
                        "item": {
                            "type": "function_call",
                            "status": "completed",
                            "name": name,
                            "call_id": call_id,
                            "arguments": json.dumps(args),
                        },
                    },
                }
            )

            async def completed():
                if name == "end_call":
                    await asyncio.sleep(self.end_completion_delay)
                await send(
                    {
                        "type": "response.event",
                        "delegation_id": call_id,
                        "event": {"type": "response.completed", "response": {"output": []}},
                    }
                )

            self.background.append(asyncio.create_task(completed()))

        try:
            async for raw in ws:
                data = json.loads(raw)
                self.received.append(data["type"])
                if self.close_requested:
                    self.post_close_events.append(data["type"])
                    await send(
                        {
                            "type": "error",
                            "error": {
                                "type": "invalid_request_error",
                                "message": "The session is closing and cannot accept new client events.",
                            },
                        }
                    )
                    continue
                if data["type"] == "session.start":
                    await asyncio.sleep(self.start_delay)
                    await send(
                        {
                            "type": "session.started",
                            "session": {"id": "live_fixture", "model": "gpt-live-1"},
                        }
                    )
                elif data["type"] == "session.input_audio.append":
                    samples = np.frombuffer(base64.b64decode(data["audio"]), dtype="<i2")
                    value = float(np.median(samples)) if len(samples) else 0
                    marker = round(value / 1000)
                    if marker == 1 and self.stage == 0:
                        self.stage = 1
                        await caption("user", "Tell me a long story about a museum.")
                        self.speaker = asyncio.create_task(
                            speak("A museum stood beside the river. ", 600)
                        )
                    elif marker == 2 and self.stage == 1:
                        self.stage = 2
                        self.interrupted = bool(self.speaker and not self.speaker.done())
                        if self.speaker:
                            self.speaker.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await self.speaker
                        await caption(
                            "user", "Stop the story. Look up what time the museum closes."
                        )
                        await call("lookup_hours", {"place": "museum"})
                    elif marker == 3 and self.stage == 2:
                        self.stage = 3
                        await caption("user", "Thank you. End the call now.")
                        await call("end_call", {})
                elif data["type"] == "response.item.create":
                    item = data.get("item", {})
                    if item.get("call_id") == "call_lookup_hours":
                        self.speaker = asyncio.create_task(
                            speak("The museum closes at five PM.", 15)
                        )
                elif data["type"] == "response.create":
                    self.continuations += 1
                    for kind in ["response.created", "response.completed"]:
                        await send(
                            {
                                "type": "response.event",
                                "delegation_id": f"continuation_{self.continuations}",
                                "event": {"type": kind, "response": {"output": []}},
                            }
                        )
                elif data["type"] == "session.close":
                    self.close_requested = True

                    async def close():
                        await asyncio.sleep(self.close_delay)
                        if not self.omit_closed:
                            response = {"type": "session.closed", "reason": "client_request"}
                            if not self.omit_usage:
                                response["usage"] = {"seconds": 3.0}
                            await send(response)

                    self.background.append(asyncio.create_task(close()))
        finally:
            for task in self.background:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            if self.speaker:
                self.speaker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.speaker
