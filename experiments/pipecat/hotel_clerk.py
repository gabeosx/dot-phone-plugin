"""Owned synthetic outbound hotel's clerk; publishes unseen mail after connection."""

import asyncio
import hashlib
import json
import secrets
import time
from pathlib import Path

from owned_bot import CallerScenario


def read_marker(path):
    # Docker's shared host directory can briefly expose a rename in progress.
    # Retry within the existing bounded wait; malformed data never satisfies it.
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


class HotelClerkScenario(CallerScenario):
    def __init__(self, ws, stream_sid, assets, record, *, decision_marker=None, receiver_sid=None):
        super().__init__(
            ws,
            stream_sid,
            assets,
            record,
            decision_marker=decision_marker,
            receiver_sid=receiver_sid,
        )
        self.hotel = json.loads((assets / "hotel-scenario.json").read_text())
        if self.hotel["max_seconds"] != 180:
            raise ValueError("hotel fixture duration must match owned scope")
        self.media_limit_seconds = self.hotel["max_seconds"]
        variants = [
            p
            for p in sorted((assets / "clerk-variants").iterdir())
            if p.is_dir() and not p.is_symlink()
        ]
        replay = self.hotel.get("replay_variant")
        if replay is not None and replay not in {v.name for v in variants}:
            raise ValueError("unknown owned hotel regression variant")
        self.clerk_variant = (
            next(v for v in variants if v.name == replay)
            if replay is not None
            else secrets.choice(variants)
        )
        self.question_order = secrets.SystemRandom().sample(["identity", "dates", "reference"], 3)
        record.update(
            scenario_mode="outbound_hotel",
            scenario_variant=self.clerk_variant.name,
            variant_selection="replay_failed_random_variant" if replay else "random_at_connection",
            question_order=self.question_order,
            expected_controller_call_id=self.hotel["call_id"],
        )

    async def speak(self, name):
        original = self.assets
        if name == "missing":
            self.assets = self.clerk_variant
        try:
            return await super().speak(name)
        finally:
            self.assets = original

    def publish_new_mail(self):
        source = self.clerk_variant / "late-message.json"
        message = json.loads(source.read_text())
        message["sent_at"] = time.time()
        mailbox = Path(self.hotel["mailbox_path"])
        if not mailbox.is_dir() or mailbox.is_symlink():
            raise ValueError("owned synthetic mailbox unavailable")
        path = mailbox / (message["id"] + ".json")
        if path.exists():
            raise ValueError("late mail must not exist before this call")
        raw = json.dumps(message, indent=2) + "\n"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(raw)
        temporary.chmod(0o600)
        temporary.replace(path)
        self.record["late_mail"] = {
            "message_id": message["id"],
            "created_at_epoch": message["sent_at"],
            "created_at_seconds": self.now(),
            "source_sha256": hashlib.sha256(raw.encode()).hexdigest(),
        }

    async def wait_marker(self, suffix):
        marker = self.decision_marker.parent / (self.receiver_sid + suffix)
        async with asyncio.timeout(40):
            while not self.stopped.is_set():
                if marker.exists():
                    value = read_marker(marker)
                    if (
                        value
                        and value.get("call_id") == self.hotel["call_id"]
                        and value.get("receiver_sid") == self.receiver_sid
                    ):
                        return value
                await asyncio.sleep(0.025)
        raise RuntimeError("carrier ended before hotel marker")

    async def heard_answer(self, name, after):
        await self.wait_voice(after, timeout=15)
        await self.wait_quiet(1.0, timeout=25)
        self.record.setdefault("answers", []).append(
            {"question": name, "finished_at_seconds": self.now()}
        )

    async def scenario(self):
        self.publish_new_mail()
        try:
            await self.wait_voice(-1, timeout=6)
            self.record["greeting_heard"] = True
            await self.wait_quiet(1.0, timeout=20)
        except TimeoutError:
            self.record["greeting_heard"] = False
        for name in self.question_order:
            await self.speak(name)
            await self.heard_answer(name, self.record["turns"][-1]["last_voice_sent_at_seconds"])
        await self.speak("missing")
        read = await self.wait_marker("-context-read.json")
        self.record["source_read_marker"] = read
        # Earlier holding speech is not the answer to the newly delivered source.
        # Wait for fresh carrier audio after observing the actual read delivery.
        await self.heard_answer("missing", self.now())
        reservation = {
            "synthetic": True,
            "call_id": self.hotel["call_id"],
            "reference": "734261",
            "primary_guest": "Sam Rivera",
            "authorized_guest": "Jordan Lee",
            "guest_may_check_in_without_primary": True,
            "photo_id_required": True,
            "check_in": "2026-10-22",
            "check_out": "2026-10-25",
            "updated_at_seconds": self.now(),
        }
        state_path = self.decision_marker.parent / "hotel-reservation-state.json"
        state_path.write_text(json.dumps(reservation, indent=2) + "\n")
        await self.speak("confirm")
        completion = await self.wait_marker("-hotel-confirmed.json")
        self.record["confirmed_marker"] = completion
        await self.heard_answer("confirm", self.now())
        await self.speak("hangup")
        await asyncio.wait_for(self.stopped.wait(), timeout=20)

    async def run(self):
        tasks = [asyncio.create_task(self.receive()), asyncio.create_task(self.scenario())]
        try:
            async with asyncio.timeout(self.media_limit_seconds):
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
                if self.stopped.is_set() and any(
                    t["name"] == "hangup" for t in self.record["turns"]
                ):
                    self.record["state"] = "completed"
                else:
                    raise RuntimeError("hotel carrier ended before hangup")
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


class RealMailHotelClerkScenario(HotelClerkScenario):
    """Owned mock only; never publishes synthetic source mail or calls a business."""

    def __init__(self, ws, stream_sid, assets, record, *, decision_marker=None, receiver_sid=None):
        CallerScenario.__init__(
            self,
            ws,
            stream_sid,
            assets,
            record,
            decision_marker=decision_marker,
            receiver_sid=receiver_sid,
        )
        self.hotel = json.loads((assets / "hotel-scenario.json").read_text())
        if self.hotel.get("mode") not in {"real_mail_mock", "real_mail_phone_flow", "real_mail_keypad_flow"} or self.hotel["max_seconds"] != 180:
            raise ValueError("invalid real-mail owned scope")
        self.media_limit_seconds = 180
        self.question_order = secrets.SystemRandom().sample(["identity", "dates", "reference"], 3)
        self.live_order = secrets.SystemRandom().sample(["room", "routing"], 2) + ["gmail"]
        if self.hotel["mode"] in {"real_mail_phone_flow", "real_mail_keypad_flow"}:
            # Exercise both providers while leaving time for a real hold and clarification.
            # Historical routing is covered by the complete warmup baseline.
            self.question_order = secrets.SystemRandom().sample(["dates", "reference"], 2)
            self.live_order = ["room", "gmail"]
        self.menu_digit = secrets.choice(["2", "5", "7"]) if self.hotel["mode"] == "real_mail_keypad_flow" else None
        record["menu_digit_selected_after_stream_open"] = self.menu_digit
        self.used_reads = set()
        record.update(
            scenario_mode="owned_historical_real_mail",
            question_order=self.question_order,
            live_order=self.live_order,
            expected_controller_call_id=self.hotel["call_id"],
            real_hotel_called=False,
            clock_origin_epoch=time.time(),
        )

    async def speak(self, name):
        return await CallerScenario.speak(self, name)

    async def fresh_read(self, after_epoch):
        marker = self.decision_marker.parent / (self.receiver_sid + "-context-read.json")
        async with asyncio.timeout(45):
            while not self.stopped.is_set():
                if marker.exists():
                    value = read_marker(marker)
                    if not value:
                        await asyncio.sleep(0.025)
                        continue
                    rid = value.get("request_id")
                    if (
                        value.get("call_id") == self.hotel["call_id"]
                        and value.get("receiver_sid") == self.receiver_sid
                        and rid not in self.used_reads
                        and value.get("request_created_ms", 0) >= after_epoch * 1000
                    ):
                        self.used_reads.add(rid)
                        return value
                await asyncio.sleep(0.025)
        raise RuntimeError("carrier ended before fresh real-mail read")

    async def wait_confirmation_or_retry(self):
        async with asyncio.timeout(40):
            while not self.stopped.is_set():
                for suffix, status in [
                    ("-hotel-confirmed.json", "confirmed"),
                    ("-confirmation-retry.json", "retry"),
                ]:
                    value = read_marker(self.decision_marker.parent / (self.receiver_sid + suffix))
                    if (
                        value
                        and value.get("call_id") == self.hotel["call_id"]
                        and value.get("receiver_sid") == self.receiver_sid
                    ):
                        return status, value
                await asyncio.sleep(0.025)
        raise RuntimeError("carrier ended before mock confirmation")

    async def scenario(self):
        if self.hotel["mode"] in {"real_mail_phone_flow", "real_mail_keypad_flow"}:
            if self.menu_digit:
                await self.speak("menu_keypad_" + self.menu_digit)
                await asyncio.wait_for(self.key_received.wait(), timeout=25)
                keys = self.record["received_keys"]
                if len(keys) != 1 or keys[0]["digit"] != self.menu_digit:
                    raise RuntimeError("incorrect or repeated actual keypad selection")
                self.record["keypad_menu_passed"] = True
                # The recipient's continuation proves acceptance. Allow the
                # originating stream to reattach before speaking again.
                await asyncio.sleep(2)
            else:
                await self.speak("menu")
                await self.heard_answer("menu", self.record["turns"][-1]["last_voice_sent_at_seconds"])
            await self.speak("hold")
            hold_start = self.now()
            await asyncio.sleep(8)
            self.record["hold_interval"] = {"started_seconds": hold_start, "ended_seconds": self.now(), "acknowledgment_allowed_until": hold_start + 3}
            await self.speak("identity")
            await self.heard_answer("identity", self.record["turns"][-1]["last_voice_sent_at_seconds"])
        try:
            await self.wait_voice(-1, timeout=6)
            self.record["greeting_heard"] = True
            await self.wait_quiet(1.0, timeout=20)
        except TimeoutError:
            self.record["greeting_heard"] = False
        for name in self.question_order:
            await self.speak(name)
            await self.heard_answer(name, self.record["turns"][-1]["last_voice_sent_at_seconds"])
        if self.hotel["mode"] in {"real_mail_phone_flow", "real_mail_keypad_flow"}:
            await self.speak("clarify")
            await self.heard_answer("clarify", self.record["turns"][-1]["last_voice_sent_at_seconds"])
        for name in self.live_order:
            after_epoch = time.time()
            await self.speak(name)
            read = await self.fresh_read(after_epoch)
            self.record.setdefault("live_source_markers", []).append({"question": name, **read})
            await self.heard_answer(name, self.now() - 0.1)
        reservation = {
            "synthetic": True,
            "real_reservation_changed": False,
            "call_id": self.hotel["call_id"],
            "reference": "93105211",
            "primary_guest": "Alex Morgan",
            "authorized_guest": "Jordan Lee",
            "guest_may_check_in_without_primary": True,
            "photo_id_required": True,
            "check_in": "2026-10-08",
            "check_out": "2026-10-12",
            "updated_at_seconds": self.now(),
        }
        (self.decision_marker.parent / "hotel-reservation-state.json").write_text(
            json.dumps(reservation)
        )
        await self.speak("confirm")
        status, marker = await self.wait_confirmation_or_retry()
        if status == "retry":
            self.record["confirmation_retry_marker"] = marker
            await self.heard_answer("confirmation_clarification", self.now() - 0.1)
            await self.speak("confirm_repeat")
            marker = await self.wait_marker("-hotel-confirmed.json")
        self.record["confirmed_marker"] = marker
        await self.heard_answer("confirm", self.now() - 0.1)
        await self.speak("hangup")
        await asyncio.wait_for(self.stopped.wait(), timeout=20)
