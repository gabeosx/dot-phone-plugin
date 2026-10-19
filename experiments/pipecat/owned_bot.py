"""An authenticated, bounded synthetic caller for two account-owned numbers."""

import asyncio
import audioop
import base64
import json
import re
import secrets
import time
import wave
from pathlib import Path

from dots_phone.twilio_security import valid_media_signature
from fastapi import APIRouter, Request, WebSocket
from fastapi.responses import Response
from twilio.twiml.voice_response import VoiceResponse


def clip_audio(path):
    with wave.open(str(path)) as source:
        if source.getnchannels() != 1 or source.getsampwidth() != 2:
            raise ValueError("synthetic fixtures must be mono PCM16")
        pcm, _ = audioop.ratecv(
            source.readframes(source.getnframes()), 2, 1, source.getframerate(), 8000, None
        )
        return audioop.lin2ulaw(pcm, 2)


class CallerScenario:
    def __init__(
        self, websocket, stream_sid, assets, record, *, decision_marker=None, receiver_sid=None
    ):
        self.ws = websocket
        self.stream_sid = stream_sid
        self.assets = assets
        self.record = record
        self.variant_assets = None
        variants = self.assets / "variants"
        if variants.is_dir():
            choices = [
                p
                for p in sorted(variants.iterdir())
                if p.is_dir()
                and not p.is_symlink()
                and (p / "interrupt.wav").is_file()
                and (p / "followup.wav").is_file()
            ]
            if not choices or len(choices) > 8:
                raise ValueError("invalid blinded caller variants")
            self.variant_assets = secrets.choice(choices)
            self.record["scenario_variant"] = self.variant_assets.name
        self.decision_marker, self.receiver_sid = decision_marker, receiver_sid
        self.followup = bool(self.variant_assets or (self.assets / "followup.wav").exists())
        self.mail_only = (self.assets / "mail-only.signal").is_file()
        self.forwarded_requests = set()
        self.start = time.monotonic()
        self.record["clock_origin_monotonic"] = self.start
        self.last_voice = -100.0
        self.marks = {}
        self.output = bytearray()
        self.media_limit_seconds = 90
        self.energy = []
        self.stopped = asyncio.Event()
        self.key_received = asyncio.Event()

    def now(self):
        return time.monotonic() - self.start

    async def receive(self):
        while True:
            event = await self.ws.receive_json()
            if event.get("streamSid") != self.stream_sid:
                raise ValueError("unexpected stream identity")
            kind = event.get("event")
            if kind == "stop":
                self.record["stream_stopped"] = True
                self.stopped.set()
                return
            if kind == "mark":
                mark = self.marks.get(event.get("mark", {}).get("name"))
                if mark:
                    mark.set()
            elif kind == "dtmf":
                dtmf = event.get("dtmf", {})
                if dtmf.get("track") == "inbound" and re.fullmatch(
                    r"[0-9#*]", dtmf.get("digit", "")
                ):
                    self.record.setdefault("received_keys", []).append(
                        {
                            "digit": dtmf["digit"],
                            "at_seconds": round(self.now(), 4),
                            "source": "Twilio inbound DTMF event",
                        }
                    )
                    self.key_received.set()
            elif kind == "media":
                data = event["media"]
                if "first_media_at_seconds" not in self.record:
                    self.record["first_media_at_seconds"] = round(self.now(), 3)
                    self.record["first_media_timestamp_ms"] = int(data["timestamp"])
                pcm = audioop.ulaw2lin(base64.b64decode(data["payload"], validate=True), 2)
                position = int(data["timestamp"]) * 16
                if position < 0 or position + len(pcm) > self.media_limit_seconds * 8000 * 2:
                    raise ValueError("media duration limit exceeded")
                if position > len(self.output):
                    self.output.extend(b"\0" * (position - len(self.output)))
                end = position + len(pcm)
                if end > len(self.output):
                    self.output.extend(b"\0" * (end - len(self.output)))
                self.output[position:end] = pcm
                self.energy.append(
                    {
                        "at_seconds": round(self.now(), 4),
                        "timestamp_ms": int(data["timestamp"]),
                        "duration_ms": len(pcm) / 16,
                        "rms": audioop.rms(pcm, 2),
                    }
                )
                if audioop.rms(pcm, 2) > 200:
                    self.last_voice = self.now()

    async def wait_voice(self, after, timeout=18):
        async with asyncio.timeout(timeout):
            while self.last_voice <= after:
                if self.stopped.is_set():
                    raise RuntimeError("carrier stopped before expected response")
                await asyncio.sleep(0.02)

    async def wait_quiet(self, quiet_seconds=0.8, timeout=12):
        async with asyncio.timeout(timeout):
            while self.now() - self.last_voice < quiet_seconds:
                await asyncio.sleep(0.02)

    async def wait_active_speech(self, after, timeout=20):
        async with asyncio.timeout(timeout):
            while True:
                now = self.now()
                voiced = sum(
                    s["duration_ms"]
                    for s in self.energy
                    if max(after, now - 2.8) <= s["at_seconds"] <= now and s["rms"] > 200
                )
                if now - self.last_voice < 0.03 and self.energy[-1]["rms"] > 200 and voiced >= 2000:
                    return
                if self.stopped.is_set():
                    raise RuntimeError("carrier stopped before sustained response speech")
                await asyncio.sleep(0.02)

    async def speak(self, name):
        source = (
            self.variant_assets
            if self.variant_assets and name in {"interrupt", "followup"}
            else self.assets
        )
        clip = clip_audio(source / (name + ".wav"))
        turn = {"name": name, "at_seconds": round(self.now(), 3)}
        self.record["turns"].append(turn)
        mark = asyncio.Event()
        self.marks[name] = mark
        next_at = time.monotonic()
        for offset in range(0, len(clip), 160):
            chunk = clip[offset : offset + 160].ljust(160, b"\xff")
            if audioop.rms(audioop.ulaw2lin(chunk, 2), 2) > 200:
                turn.setdefault("first_voice_sent_at_seconds", round(self.now(), 4))
                turn["last_voice_sent_at_seconds"] = round(self.now() + 0.02, 4)
            await self.ws.send_json(
                {
                    "event": "media",
                    "streamSid": self.stream_sid,
                    "media": {"payload": base64.b64encode(chunk).decode()},
                }
            )
            next_at += 0.02
            await asyncio.sleep(max(0, next_at - time.monotonic()))
        await self.ws.send_json(
            {"event": "mark", "streamSid": self.stream_sid, "mark": {"name": name}}
        )
        await asyncio.wait_for(mark.wait(), timeout=10)
        turn["playback_completed_at_seconds"] = round(self.now(), 4)
        self.record["playback_marks"].append(name)
        return self.now()

    async def wait_supervisor(self):
        # This local test-harness marker only controls when the synthetic caller
        # speaks again. It supplies no answer and invokes no phone/model tool.
        async with asyncio.timeout(60 if self.followup else 35):
            while True:
                if self.stopped.is_set():
                    raise RuntimeError("carrier stopped before supervisor answer")
                if self.decision_marker.exists():
                    marker = json.loads(self.decision_marker.read_text())
                    if (
                        marker.get("receiver_sid") == self.receiver_sid
                        and marker.get("decision_forwarded") is True
                        and marker.get("request_id") not in self.forwarded_requests
                    ):
                        self.forwarded_requests.add(marker.get("request_id"))
                        self.record["supervisor_forwarding_observed_at_seconds"] = round(
                            self.now(), 4
                        )
                        self.record.setdefault("supervisor_forwardings", []).append(
                            {
                                "request_id": marker.get("request_id"),
                                "at_seconds": self.record[
                                    "supervisor_forwarding_observed_at_seconds"
                                ],
                            }
                        )
                        return
                await asyncio.sleep(0.05)

    async def scenario(self):
        try:
            await self.wait_voice(-1, timeout=4)
            self.record["greeting_heard"] = True
            await self.wait_quiet()
        except TimeoutError:
            # The callee may greet before Twilio attaches the caller's stream.
            self.record["greeting_heard"] = False
        if not self.mail_only:
            after = await self.speak("story")
            await self.wait_active_speech(after)
            self.record["voice_at_interruption"] = self.now() - self.last_voice < 0.35
        else:
            self.record["scenario_mode"] = "mail_questions_only"
        after = await self.speak("interrupt")
        if self.decision_marker is not None:
            await self.wait_supervisor()
            # The watcher can observe forwarding just after answer audio begins.
            # An earlier holding sentence does not satisfy this wait.
            await self.wait_voice(self.now() - 1, timeout=5)
        else:
            await self.wait_voice(after, timeout=35)
            await asyncio.sleep(7)
        await self.wait_quiet(1.2, timeout=24 if self.followup else 12)
        await asyncio.sleep(0.5)
        if self.followup:
            await self.speak("followup")
            await self.wait_supervisor()
            await self.wait_voice(self.now() - 1, timeout=5)
            await self.wait_quiet(1.2, timeout=24)
            await asyncio.sleep(0.5)
        await self.speak("hangup")
        await asyncio.wait_for(self.stopped.wait(), timeout=20)

    async def run(self):
        tasks = [asyncio.create_task(self.receive()), asyncio.create_task(self.scenario())]
        try:
            async with asyncio.timeout(90 if self.followup else 70):
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
                if self.stopped.is_set() and any(
                    t["name"] == "hangup" for t in self.record["turns"]
                ):
                    self.record["state"] = "completed"
                else:
                    raise RuntimeError("carrier ended before the hangup turn")
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


def create_owned_bot_router(
    *, origin, caller, number, account_sid, validator, assets, directory, supervised=False
):
    router = APIRouter()
    records = {}
    directory = Path(directory)
    # Validate the recording/status destination during setup, before a carrier
    # request can reserve a call and fail in the webhook's first status write.
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)

    def save():
        safe = {sid: {k: v for k, v in r.items() if k != "permit"} for sid, r in records.items()}
        path = directory / "owned-bot-status.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"synthetic_only": True, "calls": safe}, indent=2))
        temporary.replace(path)

    @router.post("/twilio/owned-bot")
    async def webhook(request: Request):
        if request.url.query or len(await request.body()) > 16384:
            return Response(status_code=403)
        data = dict(await request.form())
        if not validator.validate(
            origin + "/twilio/owned-bot", data, request.headers.get("x-twilio-signature", "")
        ) or (data.get("AccountSid"), data.get("From"), data.get("To")) != (
            account_sid,
            caller,
            number,
        ):
            return Response(status_code=403)
        sid = data.get("CallSid", "")
        if not sid.startswith("CA") or len(sid) != 34:
            return Response(status_code=403)
        response = VoiceResponse()
        if (
            sid in records
            or len(records) >= 2
            or any(r["state"] in {"reserved", "active"} for r in records.values())
        ):
            response.hangup()
        else:
            record = {
                "state": "reserved",
                "permit": secrets.token_urlsafe(32),
                "turns": [],
                "playback_marks": [],
                "stream_stopped": False,
                "voice_at_interruption": False,
            }
            records[sid] = record
            save()
            stream = response.connect().stream(
                url=origin.replace("https://", "wss://", 1) + "/owned-bot/media"
            )
            stream.parameter(name="permit", value=record["permit"])
            response.hangup()
        return Response(str(response), media_type="application/xml")

    @router.websocket("/owned-bot/media")
    async def media(ws: WebSocket):
        if ws.url.query or not valid_media_signature(
            validator, origin, "/owned-bot/media", ws.headers.get("x-twilio-signature", "")
        ):
            await ws.close(code=1008)
            return
        await ws.accept()
        message = await asyncio.wait_for(ws.receive_json(), timeout=5)
        if message.get("event") == "connected":
            message = await asyncio.wait_for(ws.receive_json(), timeout=5)
        start = message.get("start", {}) if message.get("event") == "start" else {}
        sid = start.get("callSid")
        record = records.get(sid)
        if (
            not record
            or record["state"] != "reserved"
            or start.get("accountSid") != account_sid
            or not secrets.compare_digest(
                start.get("customParameters", {}).get("permit", ""), record["permit"]
            )
            or start.get("mediaFormat")
            != {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1}
        ):
            await ws.close(code=1008)
            return
        record["state"] = "active"
        save()
        scenario_class = CallerScenario
        if (Path(assets) / "account-scenario.json").is_file():
            from account_clerk import AccountClerkScenario, LookupAccountClerkScenario

            config = json.loads((Path(assets) / "account-scenario.json").read_text())
            scenario_class = (
                LookupAccountClerkScenario
                if config.get("mode") == "live_lookup"
                else AccountClerkScenario
            )
        if (Path(assets) / "hotel-scenario.json").is_file():
            from hotel_clerk import HotelClerkScenario, RealMailHotelClerkScenario

            config = json.loads((Path(assets) / "hotel-scenario.json").read_text())
            scenario_class = (
                RealMailHotelClerkScenario
                if config.get("mode")
                in {"real_mail_mock", "real_mail_phone_flow", "real_mail_keypad_flow"}
                else HotelClerkScenario
            )
        scenario = scenario_class(
            ws,
            start["streamSid"],
            Path(assets),
            record,
            decision_marker=directory / (sid + "-decision-forwarded.json") if supervised else None,
            receiver_sid=sid,
        )
        try:
            await scenario.run()
        except Exception as error:
            record.update(state="failed", error_type=type(error).__name__)
        finally:
            record["duration_seconds"] = round(scenario.now(), 3)
            record["received_audio_seconds"] = round(len(scenario.output) / 16000, 3)
            with wave.open(str(directory / (sid + "-received.wav")), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(8000)
                output.writeframes(scenario.output)
            (directory / (sid + "-energy.json")).write_text(json.dumps(scenario.energy))
            save()
            try:
                await ws.close()
            except Exception:
                pass

    return router
