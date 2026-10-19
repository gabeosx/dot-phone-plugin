import asyncio
import base64
import json
import os
import stat
import time
import wave
from unittest.mock import AsyncMock

import pytest

from dots_phone.call_evidence import CallRecording, RecordingSocket, render_recording
from dots_phone.export_evidence import export


def media(value=0, count=160):
    return json.dumps(
        {"event": "media", "media": {"payload": base64.b64encode(bytes([value]) * count).decode()}}
    )


async def test_actual_socket_audio_survives_replacement_export_and_clear(service, brief, tmp_path):
    cid = service.prepare("owner", brief)["call_id"]
    clock = [10.0]
    recording = CallRecording(service, service.call("owner", cid), clock=lambda: clock[0])
    first = AsyncMock()
    first.receive.return_value = {"type": "websocket.receive", "text": media()}
    socket = RecordingSocket(first, recording)
    await socket.receive()
    # 200 ms outbound queued, then clear at 100 ms. The unplayed tail is removed.
    await socket.send_text(media(count=1600))
    clock[0] += 0.1
    await socket.send_text('{"event":"clear"}')
    second = AsyncMock()
    replacement = RecordingSocket(second, recording)
    await replacement.send_text(media(value=128))
    clock[0] += 1
    recording.finish()
    recording.finish()  # Idempotent finalization.
    assert service.call("owner", cid)["recording"]["state"] == "complete"
    call = service.call("owner", cid)
    call["private_keypad_commands"] = {
        "fixture": {
            "digits": "0710",
            "carrier_accepted": True,
            "recipient_delivery_confirmed": False,
        }
    }
    service.save(call)
    path = tmp_path / "private-export"
    previous_umask = os.umask(0o022)
    os.umask(previous_umask)
    result = export(service.settings, cid, path)
    assert os.umask(previous_umask) == previous_umask
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in path.iterdir())
    assert result["recording"]["assistant_bytes"] == 1760
    commands = json.loads((path / "keypad-commands.json").read_text())
    assert commands[0]["digits"] == "0710"
    assert commands[0]["recipient_delivery_confirmed"] is False
    assert "private_keypad_commands" not in service.get("owner", cid)
    with wave.open(str(path / "conversation.wav")) as wav:
        assert wav.getnchannels() == 2 and wav.getframerate() == 8000
        from array import array

        samples = array("h")
        samples.frombytes(wav.readframes(wav.getnframes()))
    assert samples[0] != 0 and samples[1] != 0
    assert samples[800 * 2 + 1] > 0  # Replacement audio after clear.
    assert samples[1000 * 2 + 1] == 0  # No discarded tail replay.
    # Sensitive packets are encrypted, not plaintext JSON in SQLite.
    raw = service.store.db.execute(
        "SELECT payload FROM objects WHERE kind='call_audio'"
    ).fetchone()[0]
    assert b'"packets"' not in raw
    service.call("owner", cid)  # Read-only exporter did not take the process lock.


async def test_failed_send_is_not_recorded_and_no_incomplete_export(service, brief, tmp_path):
    cid = service.prepare("owner", brief)["call_id"]
    recording = CallRecording(service, service.call("owner", cid))
    socket = RecordingSocket(AsyncMock(), recording)
    socket.socket.send_text.side_effect = OSError("transport failed")
    with pytest.raises(OSError):
        await socket.send_text(media())
    assert recording.metadata["assistant_bytes"] == 0
    with pytest.raises(ValueError, match="complete recording unavailable"):
        export(service.settings, cid, tmp_path / "incomplete")
    recording.finish()


async def test_recording_storage_failure_ends_call_instead_of_silent_loss(service, brief):
    cid = service.prepare("owner", brief)["call_id"]
    clock = [0.0]
    recording = CallRecording(service, service.call("owner", cid), clock=lambda: clock[0])
    service.end = AsyncMock()
    socket = RecordingSocket(AsyncMock(), recording)
    original = service.store.put
    try:
        service.store.put = lambda *args: (_ for _ in ()).throw(OSError("disk full"))
        clock[0] = 2.0
        with pytest.raises(RuntimeError, match="private call recording failed"):
            await socket.send_text(media())
        await asyncio.sleep(0)
        service.end.assert_awaited_once_with("owner", cid, "recording_failed")
        assert recording.failed
    finally:
        service.store.put = original
    recording.finish()
    assert service.call("owner", cid)["recording"]["state"] == "incomplete"


def test_expiration_removes_audio_and_transcript_together(service, brief):
    cid = service.prepare("owner", brief)["call_id"]
    call = service.call("owner", cid)
    recording = CallRecording(service, call)
    recording.observe(media(), "recipient")
    recording.finish()
    call = service.call("owner", cid)
    call.update(state="completed", terminal_at=time.time() - 90000)
    service.save(call)
    service.expire_records()
    assert service.store.get("call", cid) is None
    assert service.store.all("call_audio") == []


def test_missing_chunk_never_renders_as_complete():
    with pytest.raises(ValueError, match="count mismatch"):
        render_recording({"chunks": 1, "duration_ms": 10}, [])


async def test_carrier_replacement_records_real_stream_io(service, brief):
    from dots_phone.carrier_stream import CarrierStream
    from tests.test_carrier_stream import Socket

    cid = service.prepare("owner", brief)["call_id"]
    recording = CallRecording(service, service.call("owner", cid))
    first, second = Socket(), Socket()
    stream = CarrierStream(RecordingSocket(first, recording), "stream-first")
    first.input.put_nowait({"type": "websocket.receive", "text": media()})
    await asyncio.wait_for(stream.receive(), 1)
    nonce = stream.begin_change()
    await stream.send_text(media())  # Must never record speech suppressed in the gap.
    await stream.attach(RecordingSocket(second, recording), "stream-second", nonce)
    await stream.send_text(media())
    await stream.close()
    recording.finish()
    assert recording.metadata["recipient_bytes"] == 160
    assert recording.metadata["assistant_bytes"] == 160
