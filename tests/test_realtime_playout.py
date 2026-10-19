"""No-dial regression for Realtime speech surviving disagreement with local VAD."""

import asyncio
import base64
import json
from unittest.mock import MagicMock

import pytest
from pydantic import SecretStr
from starlette.websockets import WebSocketState

pytest.importorskip("pipecat")

from pipecat.audio.vad.vad_analyzer import VADState

from dots_phone.pipecat_voice import PipecatTelephony


class MemoryCarrier:
    """ASGI socket with no listener, DNS, carrier API, or network connection."""

    def __init__(self, start):
        self.headers = {}
        self.application_state = self.client_state = WebSocketState.CONNECTED
        self.incoming = asyncio.Queue()
        self.sent = []
        self.feed({"event": "start", "start": start})

    def feed(self, event):
        self.incoming.put_nowait({"type": "websocket.receive", "text": json.dumps(event)})

    async def accept(self):
        pass

    async def receive(self):
        return await self.incoming.get()

    async def receive_json(self):
        return json.loads((await self.receive())["text"])

    async def send_text(self, message):
        self.sent.append(json.loads(message))

    async def close(self, code=1000):
        self.application_state = self.client_state = WebSocketState.DISCONNECTED
        self.incoming.put_nowait({"type": "websocket.disconnect", "code": code})

    @property
    def audio_bytes(self):
        return sum(
            len(base64.b64decode(event["media"]["payload"]))
            for event in self.sent
            if event["event"] == "media"
        )

    @property
    def clears(self):
        return sum(event["event"] == "clear" for event in self.sent)


class MemoryRealtime:
    """Wire-format server events parsed by the actual pinned Realtime SDK."""

    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sent = []
        self.close_code = None
        self.sequence = 0
        self.emit(
            {
                "type": "session.created",
                "session": {"id": "offline-playout", "model": "gpt-realtime-2.1"},
            }
        )

    def emit(self, event):
        self.sequence += 1
        self.incoming.put_nowait(json.dumps({"event_id": f"event_{self.sequence}", **event}))

    async def __aiter__(self):
        while (message := await self.incoming.get()) is not None:
            yield message

    async def send(self, message):
        event = json.loads(message)
        self.sent.append(event)
        if event["type"] == "session.update":
            self.emit(
                {
                    "type": "session.updated",
                    "session": {**event["session"], "id": "offline-playout"},
                }
            )

    async def close(self):
        self.close_code = 1000
        self.incoming.put_nowait(None)

    def speech(self, *, started, turn):
        self.emit(
            {
                "type": f"input_audio_buffer.speech_{'started' if started else 'stopped'}",
                "item_id": f"recipient_{turn}",
                "audio_start_ms" if started else "audio_end_ms": turn * 2000,
            }
        )

    def response(self, name, *, milliseconds):
        response = {
            "id": name,
            "object": "realtime.response",
            "status": "in_progress",
            "status_details": None,
            "output": [],
        }
        self.emit({"type": "response.created", "response": response})
        self.emit(
            {
                "type": "conversation.item.added",
                "previous_item_id": None,
                "item": {
                    "id": name + "_item",
                    "type": "message",
                    "role": "assistant",
                    "phase": "final_answer",
                    "status": "in_progress",
                    "content": [],
                },
            }
        )
        # Stream 100ms PCM16 mono chunks at 24 kHz in a generation burst. The
        # actual transport resamples, buffers, and paces them for the carrier.
        for offset in range(0, milliseconds, 100):
            chunk_ms = min(100, milliseconds - offset)
            self.emit(
                {
                    "type": "response.output_audio.delta",
                    "response_id": name,
                    "item_id": name + "_item",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": base64.b64encode(b"\x00\x10" * (24 * chunk_ms)).decode(),
                }
            )
        self.emit(
            {
                "type": "response.done",
                "response": {
                    **response,
                    "status": "completed",
                    "usage": {
                        "total_tokens": 2,
                        "input_tokens": 1,
                        "output_tokens": 1,
                        "input_token_details": {
                            "text_tokens": 0,
                            "audio_tokens": 1,
                            "cached_tokens": 0,
                        },
                        "output_token_details": {"text_tokens": 0, "audio_tokens": 1},
                    },
                },
            }
        )


async def eventually(predicate, *, timeout=3):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


async def test_realtime_remote_turns_deliver_complete_audio_despite_local_vad_disagreement(
    service, settings, brief, monkeypatch
):
    from pipecat.services.openai.realtime import llm as realtime_sdk

    from dots_phone import pipecat_transport

    # This deliberately disagrees with the remote end-of-turn signal. The old
    # production wiring silently dropped all output while this stayed SPEAKING.
    class PersistentLocalSpeech:
        def __init__(self, **kwargs):
            pass

        def set_sample_rate(self, rate):
            assert rate == 8000

        async def analyze_audio(self, audio):
            return VADState.SPEAKING

        async def cleanup(self):
            pass

    model = MemoryRealtime()
    connect = MagicMock()

    async def memory_connect(**kwargs):
        connect(**kwargs)
        return model

    monkeypatch.setattr(realtime_sdk, "websocket_connect", memory_connect)
    monkeypatch.setattr(pipecat_transport, "SileroVADAnalyzer", PersistentLocalSpeech)
    settings.pipecat_engine = "realtime"
    settings.twilio_account = SecretStr("AC" + "2" * 32)
    settings.openai_key = SecretStr("offline-model-fixture")
    client = MagicMock()
    client.calls.create.side_effect = AssertionError("This test must never dial")
    telephony = PipecatTelephony(settings, service, client)
    call_id = service.prepare("owner", brief)["call_id"]
    call = service.call("owner", call_id)
    call["state"] = "active"
    call["provider_sid"] = "CA" + "1" * 32
    service.save(call)
    carrier = MemoryCarrier(
        {
            "accountSid": settings.twilio_account.get_secret_value(),
            "callSid": call["provider_sid"],
            "streamSid": "MZ" + "3" * 32,
            "customParameters": {"dots_call_id": call_id},
        }
    )
    worker = asyncio.create_task(telephony.media(carrier))
    try:
        await eventually(lambda: call["provider_sid"] in telephony.sessions)
        llm = telephony.sessions[call["provider_sid"]].llm
        await asyncio.wait_for(llm.ready.wait(), 3)
        carrier.feed(
            {"event": "media", "media": {"payload": base64.b64encode(b"\xff" * 160).decode()}}
        )
        await eventually(lambda: llm.media_progress["input_frames_received"] == 1)
        model.speech(started=True, turn=1)
        await eventually(lambda: carrier.clears >= 1)
        model.speech(started=False, turn=1)
        model.response("complete_first", milliseconds=1200)
        try:
            await eventually(lambda: carrier.audio_bytes >= 9600)
        except TimeoutError:
            pytest.fail(
                f"Only {carrier.audio_bytes}/9600 PCMU bytes reached the carrier after "
                "remote speech stopped and a complete 1.2s answer was generated"
            )
        await eventually(lambda: llm._current_audio_response is None)
        assert carrier.audio_bytes == 9600  # Exactly 1.2s PCMU, not merely nonzero.
        assert carrier.clears == 1  # One authoritative remote turn, one clear.

        # Queue a longer reply, then interrupt while it is actually playing.
        model.response("interrupted", milliseconds=3000)
        await eventually(lambda: carrier.audio_bytes >= 9600 + 1600)
        clears_before = carrier.clears
        model.speech(started=True, turn=2)
        await eventually(lambda: carrier.clears == clears_before + 1)
        interrupted_bytes = carrier.audio_bytes - 9600
        assert 1600 <= interrupted_bytes < 24000
        await asyncio.sleep(0.12)  # Three output packets: cleared audio must stay cleared.
        assert carrier.audio_bytes == 9600 + interrupted_bytes

        # Remote speech stop must allow the entire next answer to play again.
        model.speech(started=False, turn=2)
        model.response("complete_after_interruption", milliseconds=1200)
        expected = 19200 + interrupted_bytes
        await eventually(lambda: carrier.audio_bytes >= expected)
        await eventually(lambda: llm._current_audio_response is None)
        assert carrier.audio_bytes == expected
        assert carrier.clears == clears_before + 1
        assert connect.call_count == 1
        client.calls.create.assert_not_called()
        assert service.provider.dials == []
    finally:
        await carrier.close()
        try:
            await asyncio.wait_for(worker, 3)
        except TimeoutError:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    saved = service.call("owner", call_id)
    playback = saved["usage"]["carrier_playback"]
    assert playback["turn_authority"] == "realtime_server_vad"
    assert playback["received_audio_ms"] == playback["forwarded_audio_ms"] == 5400
    assert playback["suppressed_during_caller_speech_ms"] == 0
    assert playback["pipeline_interruptions"] == 2
    assert playback["clear_reasons"] == {}
    speech = saved["usage"]["realtime_speech_delivery"]
    assert speech["model_output_audio_events"] == 54
    assert speech["model_output_audio_bytes"] == speech["forwarded_audio_bytes"] == 259200
    assert saved["recording"]["assistant_bytes"] == expected
    assert saved["recording"]["state"] == "complete"
