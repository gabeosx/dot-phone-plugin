"""Encrypted carrier-boundary audio, independent of generated model audio.

Records received PCMU and successfully sent PCMU plus playback clears. This is
server/carrier boundary evidence, not an independent recipient microphone.
Chunks flush at one-second intervals; an interrupted archive stays incomplete.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time


class CallRecording:
    def __init__(self, service, call, *, clock=time.monotonic):
        self.service, self.call_id, self.owner = service, call["call_id"], call["owner"]
        self.clock, self.started = clock, clock()
        self.last_flush = self.started
        self.pending = []
        self.closed = False
        self.failed = False
        self.metadata = {
            "state": "capturing",
            "format": "pcmu/8000/mono-per-direction",
            "boundary": "received_from_carrier_and_successfully_sent_to_carrier",
            "chunks": 0,
            "recipient_bytes": 0,
            "assistant_bytes": 0,
            "started_epoch_ms": round(time.time() * 1000),
        }
        call["recording"] = self.metadata.copy()
        service.save(call)  # Verify private storage before starting the voice pipeline.

    def observe(self, text, direction):
        if self.closed:
            raise RuntimeError("recording already closed")
        event = json.loads(text)
        kind = event.get("event")
        if kind not in {"media", "clear"}:
            return
        row = {"at_ms": round((self.clock() - self.started) * 1000), "direction": direction}
        if kind == "media":
            payload = event["media"]["payload"]
            size = len(base64.b64decode(payload, validate=True))
            if size > 80000:
                raise ValueError("carrier audio packet exceeds bound")
            row["payload"] = payload
            self.metadata[direction + "_bytes"] += size
        else:
            row["clear"] = True
        self.pending.append(row)
        if self.clock() - self.last_flush >= 1 or len(self.pending) >= 100:
            self.flush()

    def flush(self):
        if not self.pending:
            return
        index = self.metadata["chunks"]
        self.service.store.put(
            "call_audio",
            f"{self.call_id}:{index:06d}",
            self.owner,
            {"call_id": self.call_id, "index": index, "packets": self.pending},
        )
        self.metadata["chunks"] += 1
        self.pending = []
        self.last_flush = self.clock()
        self.save_metadata()

    def save_metadata(self):
        current = self.service.store.get("call", self.call_id, self.owner)
        if current:
            current["recording"] = self.metadata.copy()
            self.service.save(current)

    def finish(self):
        if self.closed:
            return
        self.flush()
        self.metadata.update(
            state="incomplete" if self.failed else "complete",
            duration_ms=round((self.clock() - self.started) * 1000),
        )
        self.save_metadata()
        self.closed = True


class RecordingSocket:
    """Transparent ASGI socket proxy; the same recorder spans DTMF replacements."""

    def __init__(self, socket, recording):
        self.socket, self.recording = socket, recording

    def __getattr__(self, name):
        return getattr(self.socket, name)

    async def receive(self):
        message = await self.socket.receive()
        if message.get("text"):
            self.record(message["text"], "recipient")
        return message

    async def send_text(self, text):
        await self.socket.send_text(text)
        self.record(text, "assistant")

    def record(self, text, direction):
        try:
            self.recording.observe(text, direction)
        except Exception:
            # SDK output transport catches send exceptions. Explicitly end the
            # call if evidence storage fails, rather than continuing unrecorded.
            if not self.recording.failed:
                self.recording.failed = True
                asyncio.create_task(
                    self.recording.service.end(
                        self.recording.owner, self.recording.call_id, "recording_failed"
                    )
                )
            raise RuntimeError("private call recording failed") from None

    async def send_bytes(self, data):
        raise ValueError("Twilio recording requires text PCMU frames")


def render_recording(metadata, chunks):
    """Return 8-kHz stereo PCM: recipient left, assistant right.

    Outgoing packets are queued by the carrier, so schedule them consecutively
    until a playback clear discards the unplayed tail. Network/device latency is
    unknown. This rendering does not claim independent recipient playback proof.
    """
    import audioop
    from array import array

    samples = max(1, metadata.get("duration_ms", 0) * 8)
    tracks = {name: array("h", [0]) * samples for name in ("recipient", "assistant")}
    ends = dict.fromkeys(tracks, 0)
    expected = 0
    for chunk in chunks:
        if chunk["index"] != expected:
            raise ValueError("recording chunk missing or unordered")
        expected += 1
        for row in chunk["packets"]:
            direction, at = row["direction"], row["at_ms"] * 8
            track = tracks[direction]
            if row.get("clear"):
                stop = min(len(track), ends[direction])
                track[at:stop] = array("h", [0]) * max(0, stop - at)
                ends[direction] = at
                continue
            decoded = array("h")
            decoded.frombytes(audioop.ulaw2lin(base64.b64decode(row["payload"], validate=True), 2))
            start = max(at, ends[direction])
            end = min(samples, start + len(decoded))
            if start < samples:
                track[start:end] = decoded[: end - start]
            ends[direction] = start + len(decoded)
    if expected != metadata["chunks"]:
        raise ValueError("recording chunk count mismatch")
    stereo = array("h", [0]) * (samples * 2)
    stereo[0::2], stereo[1::2] = tracks["recipient"], tracks["assistant"]
    return stereo.tobytes()
