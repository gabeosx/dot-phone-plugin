"""Owned-endpoint rehearsal: actual menu/identifier DTMF, hold and balance correction.

Account numbers and balances are fabricated. This recipient uses no connector,
account operation or text model; a lookup question can exercise the caller's
separately authorized sources.
The caller receives neither the randomly selected menu digit nor the balance.
"""

import asyncio
import json
import secrets
import time

from owned_bot import CallerScenario


class AccountClerkScenario(CallerScenario):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.media_limit_seconds = 180

    async def wait_keys(self, offset, expected):
        async with asyncio.timeout(25):
            while True:
                received = "".join(
                    k["digit"] for k in self.record.get("received_keys", [])[offset:]
                )
                if received == expected:
                    return
                if not expected.startswith(received):
                    raise RuntimeError("unexpected_account_keypad_input")
                if self.stopped.is_set():
                    raise RuntimeError("carrier_stopped_before_account_input")
                await asyncio.sleep(0.02)

    async def scenario(self):
        digit = secrets.choice(["2", "5", "7"])
        self.record["selected_menu_digit"] = digit
        await self.speak("menu-" + digit)
        await self.wait_keys(0, digit)
        self.record["menu_verified"] = True
        await self.speak("verification")
        await self.wait_keys(1, "804217#")
        self.record["verification_verified"] = True
        await self.speak("hold")
        start = self.now()
        self.record["hold_start_seconds"] = start
        await asyncio.sleep(8)
        self.record["hold_end_seconds"] = self.now()
        self.record["hold_final_three_seconds_voiced_ms"] = sum(
            item["duration_ms"]
            for item in self.energy
            if self.now() - 3 <= item["at_seconds"] <= self.now() and item["rms"] > 200
        )
        after = await self.speak("agent")
        await self.wait_voice(after, timeout=25)
        await self.wait_quiet(1.2, timeout=25)
        await self.speak("balance")
        await asyncio.wait_for(self.stopped.wait(), timeout=30)

    async def run(self):
        tasks = [asyncio.create_task(self.receive()), asyncio.create_task(self.scenario())]
        try:
            async with asyncio.timeout(180):
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
                if self.stopped.is_set() and "balance" in self.record["playback_marks"]:
                    self.record["state"] = "completed"
                else:
                    raise RuntimeError("carrier_ended_before_account_balance")
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


class LookupAccountClerkScenario(AccountClerkScenario):
    """Ask an unprepared profile question; assess source/answer truth after the call.

    The marker supplies timing only, never an answer or permission. Waiting for a
    delivered read prevents a short lookup acknowledgement from ending the turn.
    A source read plus speech is not a verification pass: compare the complete
    recording with the retrieved source independently after termination.
    """

    async def wait_source_read(self, after_epoch_ms, *, timeout=60):
        config = json.loads((self.assets / "account-scenario.json").read_text())
        marker = self.assets.parent / "source-read.json"
        async with asyncio.timeout(timeout):
            while True:
                if self.stopped.is_set():
                    raise RuntimeError("carrier_stopped_before_live_source_read")
                try:
                    data = json.loads(marker.read_text())
                except FileNotFoundError:
                    data = {}
                if (
                    data.get("call_id") == config["call_id"]
                    and data.get("source_read_delivered") is True
                    and after_epoch_ms <= data.get("at_epoch_ms", 0) <= time.time() * 1000
                ):
                    self.record["source_read_observed_at_seconds"] = self.now()
                    self.record["source_read_request_id"] = data["request_id"]
                    return
                await asyncio.sleep(0.05)

    async def scenario(self):
        config = json.loads((self.assets / "account-scenario.json").read_text())
        verification_required = config.get("verification_required", True)
        if type(verification_required) is not bool:
            raise ValueError("verification_required must be a boolean")
        digit = secrets.choice(["2", "5", "7"])
        self.record["selected_menu_digit"] = digit
        await self.speak("menu-" + digit)
        await self.wait_keys(0, digit)
        self.record["menu_verified"] = True
        if verification_required:
            await self.speak("verification")
            await self.wait_keys(1, "804217#")
            self.record["verification_verified"] = True
            await self.speak("hold")
            await asyncio.sleep(4)
        else:
            self.record["verification_not_requested"] = True
        after = await self.speak("agent")
        await self.wait_voice(after, timeout=25)
        await self.wait_quiet(1.5, timeout=25)
        self.record["profile_question_at_epoch_ms"] = time.time() * 1000
        await self.speak("profile")
        await self.wait_source_read(self.record["profile_question_at_epoch_ms"])
        # Source forwarding may precede the marker by a fraction of a second.
        await self.wait_voice(self.now() - 1, timeout=30)
        # A delivered read can require another page before a substantive reply.
        # Do not disconnect after 25 seconds of continued legitimate work; the
        # call's independently enforced overall bound still owns the deadline.
        await self.wait_quiet(2, timeout=45)
        self.record["profile_reply_observed"] = True
        await self.speak("balance")
        await asyncio.wait_for(self.stopped.wait(), timeout=30)
