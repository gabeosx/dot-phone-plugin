"""One real-model synthetic test through the public Twilio media route; no dialing."""

import argparse
import asyncio
import audioop
import base64
import json
import time
import uuid
import wave
from pathlib import Path
from xml.etree import ElementTree

import httpx
import websockets
from twilio.request_validator import RequestValidator

from dots_phone.bws import read_private


def audio(path):
    with wave.open(str(path)) as source:
        assert source.getnchannels() == 1 and source.getsampwidth() == 2
        pcm, _ = audioop.ratecv(
            source.readframes(source.getnframes()), 2, 1, source.getframerate(), 8000, None
        )
        return audioop.lin2ulaw(pcm, 2)


async def run(args, values, result):
    validator = RequestValidator(values["TWILIO_AUTH_TOKEN"])
    sid = "CA" + uuid.uuid4().hex
    stream_sid = "MZ" + uuid.uuid4().hex
    data = {
        "AccountSid": values["TWILIO_ACCOUNT_SID"],
        "From": args.caller,
        "To": args.number,
        "CallSid": sid,
    }
    url = args.origin.rstrip("/") + "/twilio/probe"
    async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
        health = await client.get(args.origin + "/healthz")
        health.raise_for_status()
        response = await client.post(
            url, data=data, headers={"x-twilio-signature": validator.compute_signature(url, data)}
        )
        response.raise_for_status()
    xml = ElementTree.fromstring(response.text)
    stream = xml.find("./Connect/Stream")
    if stream is None:
        raise RuntimeError("Probe reservation was refused")
    permit = stream.find("Parameter").attrib["value"]
    output = bytearray()
    output_times = []
    pending = asyncio.Queue()
    origin = time.monotonic()
    sent = 0
    stopping = asyncio.Event()

    async with websockets.connect(
        stream.attrib["url"],
        additional_headers={
            "x-twilio-signature": validator.compute_signature(args.origin + "/voice", {})
        },
        open_timeout=15,
        max_size=65536,
    ) as ws:
        await ws.send(json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"}))
        await ws.send(
            json.dumps(
                {
                    "event": "start",
                    "sequenceNumber": "1",
                    "streamSid": stream_sid,
                    "start": {
                        "accountSid": values["TWILIO_ACCOUNT_SID"],
                        "callSid": sid,
                        "streamSid": stream_sid,
                        "tracks": ["inbound"],
                        "mediaFormat": {
                            "encoding": "audio/x-mulaw",
                            "sampleRate": 8000,
                            "channels": 1,
                        },
                        "customParameters": {"permit": permit},
                    },
                }
            )
        )

        async def sender():
            nonlocal sent
            next_at = time.monotonic()
            while not stopping.is_set():
                chunk = pending.get_nowait() if not pending.empty() else b"\xff" * 160
                sent += 1
                await ws.send(
                    json.dumps(
                        {
                            "event": "media",
                            "sequenceNumber": str(sent + 1),
                            "streamSid": stream_sid,
                            "media": {
                                "track": "inbound",
                                "chunk": str(sent),
                                "timestamp": str((sent - 1) * 20),
                                "payload": base64.b64encode(chunk).decode(),
                            },
                        }
                    )
                )
                next_at += 0.02
                await asyncio.sleep(max(0, next_at - time.monotonic()))

        async def receiver():
            async for raw in ws:
                event = json.loads(raw)
                if event.get("event") == "media":
                    pcm = audioop.ulaw2lin(base64.b64decode(event["media"]["payload"]), 2)
                    output.extend(pcm)
                    if audioop.rms(pcm, 2) > 200:
                        output_times.append(time.monotonic() - origin)
                elif event.get("event") == "clear":
                    result["clear_events"] += 1
            stopping.set()

        async def speak(name):
            clip = audio(args.assets / (name + ".wav"))
            result["turns"].append(
                {"name": name, "at_seconds": round(time.monotonic() - origin, 3)}
            )
            for offset in range(0, len(clip), 160):
                pending.put_nowait(clip[offset : offset + 160].ljust(160, b"\xff"))
            await asyncio.sleep(len(clip) / 8000 + 0.2)

        async def scenario():
            await asyncio.sleep(3)
            await speak("story")
            threshold = time.monotonic() - origin
            async with asyncio.timeout(15):
                while not any(t >= threshold for t in output_times):
                    await asyncio.sleep(0.05)
            await asyncio.sleep(2.5)
            result["voiced_at_interruption"] = bool(
                output_times and time.monotonic() - origin - output_times[-1] < 0.3
            )
            await speak("interrupt")
            await asyncio.sleep(7)
            await speak("hangup")

        tasks = [asyncio.create_task(sender()), asyncio.create_task(receiver())]
        try:
            async with asyncio.timeout(55):
                await scenario()
                await tasks[1]
        finally:
            stopping.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    with wave.open(str(args.output / "output.wav"), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(8000)
        target.writeframes(output)
    result.update(
        public_http_passed=True,
        public_websocket_passed=True,
        voiced_output_seconds=len(output) / 16000,
        elapsed_seconds=round(time.monotonic() - origin, 3),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--caller", required=True)
    parser.add_argument("--number", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--assets", type=Path, default=Path("artifacts/pipecat/inputs/assets"))
    args = parser.parse_args()
    args.output.mkdir(mode=0o700)
    result = {
        "mode": "synthetic_public_twilio_transport",
        "pstn_calls": 0,
        "state": "reserved",
        "clear_events": 0,
        "turns": [],
        "automatic_retry": False,
    }
    (args.output / "result.json").write_text(json.dumps(result, indent=2))
    try:
        values = json.loads(read_private(Path(".secrets/pipecat-inbound-runtime.json")))
        asyncio.run(run(args, values, result))
        result["state"] = "transport_completed"
    except Exception as error:
        result.update(state="failed", error_type=type(error).__name__)
    finally:
        (args.output / "result.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result))
    raise SystemExit(0 if result["state"] == "transport_completed" else 1)


if __name__ == "__main__":
    main()
