"""Owned synthetic telephone counterparty. Disabled without operator-mounted scope.

This service cannot dial, read owner records, or grant the production app approval.
The default test profile still runs the offline callback receiver. Activation of
this separate profile requires an approved owned route, test runtime and campaign.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import Response
from pydantic import SecretStr
from twilio.request_validator import RequestValidator

from dots_phone.bws import read_private
from dots_phone.config import Settings, live_number
from dots_phone.harness import load_cases
from dots_phone.models import Brief
from dots_phone.protocol import split_context_text
from dots_phone.service import ACTIVE, PhoneService, Refused
from dots_phone.store import Store
from dots_phone.voice import GPTLiveProvider, LiveTelephony, SupervisedConfig, SupervisedProvider

TEST_KEYS = {
    "OPENAI_API_KEY",
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_API_KEY",
    "TWILIO_API_SECRET",
}


def validate_scope(scope):
    required = {
        "campaign_id",
        "approval_reference",
        "public_base_url",
        "caller",
        "recipient",
        "expires",
        "max_calls",
        "max_seconds",
        "budget_usd",
        "scenarios",
    }
    if set(scope) != required or not scope["campaign_id"] or not scope["approval_reference"]:
        raise Refused("operator-approved test scope required")
    origin = urlsplit(scope["public_base_url"])
    cases = {c["id"] for c in load_cases()}
    if (
        origin.scheme != "https"
        or not origin.hostname
        or origin.path not in {"", "/"}
        or origin.username
        or origin.password
        or origin.query
        or origin.fragment
        or not live_number(scope["caller"])
        or not live_number(scope["recipient"])
        or scope["caller"] == scope["recipient"]
        or not time.time() < scope["expires"] <= time.time() + 86400
        or type(scope["max_calls"]) is not int
        or not 1 <= scope["max_calls"] <= 8
        or type(scope["max_seconds"]) is not int
        or not 1 <= scope["max_seconds"] <= 90
        or float(scope["budget_usd"]) < 2 * scope["max_calls"]
        or float(scope["budget_usd"]) > 20
        or not scope["scenarios"]
        or not set(scope["scenarios"]) <= cases
    ):
        raise Refused("invalid or expired owned test scope")
    return scope


def counterparty_config(case):
    # The simulator receives only its independent role and fictional facts.
    # No grading answers, owner brief, OAuth/BWS access, or production records.
    return {
        "model": "gpt-live-1",
        "delegation": {"type": "client"},
        "audio": {"format": {"type": "audio/pcmu", "rate": 8000}, "output": {"voice": "marin"}},
        "instructions": "You are an automated fictional counterparty for an owned bot-to-bot test. Briefly identify yourself as an AI test bot. Use only this role and synthetic facts. Never perform external actions, access accounts, transfer or redial. Stop when asked.\n"
        + json.dumps(case["simulator"]),
    }


class CounterpartyProvider(SupervisedProvider):
    def _register_call(self, start, websocket):
        call_id = start.get("customParameters", {}).get("dots_call_id")
        call = self.supervisor.store.get("call", call_id)
        if not call or call.get("media_started"):
            raise Refused("unknown or duplicate synthetic media session")
        conv_id = super()._register_call(start, websocket)
        call["media_started"] = True
        self.supervisor.save(call)
        self._call_session_configs[conv_id] = counterparty_config(self.cases[call["scenario_id"]])
        return conv_id

    async def _dispatch_model_event(self, conv_id, session, event):
        # Retain upstream codec/transcript/finalization handling without creating
        # a Dots decision or giving the counterparty another account's tools.
        await GPTLiveProvider._dispatch_model_event(self, conv_id, session, event)
        call = self.supervisor.store.get("call", session.metadata["dots_call_id"])
        if event.get("type") == "session.delegation.created":
            delegation = event.get("delegation", {})
            if delegation.get("target") == "client" and delegation.get("id"):
                for chunk in split_context_text(
                    "Stay within the supplied fictional role. No additional facts or external actions are available."
                ):
                    await self._model_send(
                        conv_id,
                        {
                            "type": "session.commentary.append",
                            "delegation_id": delegation["id"],
                            "content": chunk,
                        },
                    )
        if event.get("type") == "session.closed":
            seconds = event.get("usage", {}).get("seconds")
            if type(seconds) in (int, float) and 0 <= seconds <= 110:
                call["usage"].update(live_seconds=seconds, live_usage_confirmed=True)
                self.supervisor.save(call)


class CounterpartyConfig(SupervisedConfig):
    def create_provider(self, channel, tac_config):
        return CounterpartyProvider(channel, tac_config, self)


class CounterpartyTransport(LiveTelephony):
    async def dial(self, call):
        raise Refused("counterparty never dials")


def create_recipient(scope, values, directory, transport=None):
    scope = validate_scope(scope)
    if set(values) != TEST_KEYS or not all(values.values()):
        raise Refused("dedicated test runtime allowlist required")
    # Minimal synthetic-only state has a public fixture key; credentials and
    # full transcripts are never stored here. Production volumes are absent.
    settings = Settings(
        profile="evaluation",
        database=directory / "recipient.sqlite3",
        allowed_numbers=[scope["caller"]],
    )
    store = Store(settings.database, settings.encryption_key.get_secret_value())
    service = PhoneService(settings, store)
    if transport is None:
        transport_settings = SimpleNamespace(
            public_base_url=scope["public_base_url"].rstrip("/"),
            phone_number=scope["recipient"],
            **{
                name: SecretStr(values[key])
                for name, key in {
                    "openai_key": "OPENAI_API_KEY",
                    "twilio_account": "TWILIO_ACCOUNT_SID",
                    "twilio_auth": "TWILIO_AUTH_TOKEN",
                    "twilio_key": "TWILIO_API_KEY",
                    "twilio_secret": "TWILIO_API_SECRET",
                }.items()
            },
        )
        # Reuse the audited production carrier/codec/teardown transport, replacing
        # only the independent participant instructions and client delegation.
        transport = CounterpartyTransport(transport_settings, service)
        from dots_phone.voice import VoiceChannel

        transport.channel = VoiceChannel(
            transport.channel.tac,
            config=CounterpartyConfig(
                openai_api_key=values["OPENAI_API_KEY"],
                default_session_config=counterparty_config(load_cases()[0]),
                welcome_instruction="Introduce yourself briefly as an AI test bot, then wait for the calling assistant.",
            ),
        )
        transport.channel._twilio_client = transport.client
        transport.channel._provider.supervisor = service
        transport.channel._provider.cases = {c["id"]: c for c in load_cases()}
    service.provider = transport
    origin = scope["public_base_url"].rstrip("/")
    validator = RequestValidator(values["TWILIO_AUTH_TOKEN"])

    @asynccontextmanager
    async def lifespan(app):
        await service.recover()

        async def supervise():
            while True:
                for call in store.all("call"):
                    if call["state"] in {"active", "awaiting_decision"} and (
                        time.time() >= scope["expires"]
                        or time.time() >= call["dial_started"] + scope["max_seconds"]
                    ):
                        await service.end("test", call["call_id"], "synthetic_scope_exhausted")
                await service.tick()
                await asyncio.sleep(0.5)

        task = asyncio.create_task(supervise())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            for call in store.all("call"):
                if call["state"] in ACTIVE:
                    await service.end("test", call["call_id"], "shutdown")
            store.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.service = service

    @app.get("/healthz")
    async def health():
        return {
            "status": "ok",
            "profile": "owned-synthetic-recipient",
            "campaign_expired": time.time() >= scope["expires"],
        }

    @app.post("/twilio/inbound/{scenario_id}")
    async def inbound(scenario_id: str, request: Request):
        if request.url.query or len(await request.body()) > 16384:
            return Response(status_code=403)
        form = dict(await request.form())
        if (
            not validator.validate(
                origin + request.url.path, form, request.headers.get("x-twilio-signature", "")
            )
            or form.get("AccountSid") != values["TWILIO_ACCOUNT_SID"]
            or form.get("From") != scope["caller"]
            or form.get("To") != scope["recipient"]
        ):
            return Response(status_code=403)
        if (
            time.time() >= scope["expires"]
            or scenario_id not in scope["scenarios"]
            or any(c["state"] in ACTIVE for c in store.all("call"))
            or any(
                c.get("media_started") and not c["usage"].get("live_usage_confirmed")
                for c in store.all("call")
            )
        ):
            return Response(status_code=409)
        sid = form.get("CallSid", "")
        if len(sid) != 34 or not sid.startswith("CA"):
            return Response(status_code=400)
        if store.get("recipient_sid", sid) or len(store.all("call")) >= scope["max_calls"]:
            return Response(status_code=409)
        # Persist the reservation before emitting TwiML or starting any model.
        brief = Brief(
            destination=scope["caller"],
            purpose="Synthetic independent counterparty: " + scenario_id,
            deadline=datetime.fromtimestamp(scope["expires"], UTC),
            max_seconds=scope["max_seconds"],
            budget_usd="2",
            synthetic=True,
        )
        call_id = service.prepare("test", brief)["call_id"]
        call = service.call("test", call_id)
        call.update(
            provider_sid=sid,
            state="active",
            dial_started=time.time(),
            scenario_id=scenario_id,
            campaign_id=scope["campaign_id"],
        )
        service.save(call)
        store.put("recipient_sid", sid, "test", {"call_id": call_id})
        from tac.channels.voice.media_streams.twiml import generate_twiml
        from tac.models.voice import VoiceTwiMLOptionsMediaStreams

        xml = generate_twiml(
            origin.replace("https://", "wss://", 1) + "/voice",
            VoiceTwiMLOptionsMediaStreams(custom_parameters={"dots_call_id": call_id}),
        )
        return Response(xml, media_type="application/xml")

    @app.websocket("/voice")
    async def media(websocket: WebSocket):
        if websocket.url.query or not validator.validate(
            origin + "/voice", {}, websocket.headers.get("x-twilio-signature", "")
        ):
            await websocket.close(code=1008)
            return
        await transport.media(websocket)

    return app


def main():
    import uvicorn

    scope = json.loads(read_private("/run/secrets/test-campaign"))
    values = json.loads(read_private("/run/secrets/test-runtime"))
    uvicorn.run(
        create_recipient(scope, values, Path("/data")),
        host="0.0.0.0",
        port=8000,
        access_log=False,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
