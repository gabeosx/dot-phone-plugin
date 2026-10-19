"""Exercise the production preparation/file bridge with a carrier-free text fixture."""

import argparse
import asyncio
import json
from pathlib import Path

from dots_phone.call_reasoner import AutonomousAccountWorker
from dots_phone.config import Settings
from dots_phone.models import Brief, Start
from dots_phone.service import PhoneService
from dots_phone.store import Store
from dots_phone.text_eval import fixture_call


class NoCarrier:
    """Local state transitions only; no network, audio, or telephony implementation."""

    def __init__(self):
        self.answers = []

    async def dial(self, call):
        assert call["usage"]["reasoner_preparation"]["prepared"] is True
        return "synthetic-no-carrier"

    async def update(self, call, delegation, text):
        self.answers.append(json.loads(text))

    async def stop(self, call):
        pass


async def run(directory):
    directory.mkdir(mode=0o700)
    script = Path(__file__).with_name("codex_call_reasoner.mjs").resolve()
    process = await asyncio.create_subprocess_exec(
        "node",
        str(script),
        "serve",
        str(directory),
        "--no-connected-tools",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    store = None
    try:
        async with asyncio.timeout(60):
            ready = json.loads(await process.stdout.readline())
            assert ready == {
                "ready": True,
                "connected_read_tools": 0,
                "source_selection_protocol": 1,
            }
            settings = Settings(
                profile="evaluation",
                database=directory / "state.db",
                allowed_numbers=["+12125550100"],
                voice_backend="pipecat",
                pipecat_engine="realtime",
                account_tasks=True,
                local_reasoner_enabled=True,
                local_reasoner_directory=directory,
                call_authorization_mode="connected_owner",
                decision_seconds=30,
            )
            store = Store(settings.database, settings.encryption_key.get_secret_value())
            carrier = NoCarrier()
            service = PhoneService(settings, store, carrier)
            service.ready = True
            service.local_context_worker = AutonomousAccountWorker(service)
            brief = fixture_call(
                {"purpose": "Find my library loan due date.", "facts": {"library_card": "00420420"}}
            )["brief"]
            brief["actions"].remove("read_call_sources")
            prepared = service.prepare("owner", Brief.model_validate(brief), client_id="fixture")
            settings.dry_run = False  # NoCarrier cannot make a network call.
            service.start(
                "owner",
                Start(
                    **{key: prepared[key] for key in ("call_id", "revision", "authorization_id")}
                ),
                client_id="fixture",
            )
            await service.tick()
            cid = prepared["call_id"]
            call = service.call("owner", cid)
            assert call["state"] == "active", call.get("stop_reason")
            service.observe_transcript(
                cid, [{"role": "user", "text": "What is your library card? Please say it."}]
            )
            pending = service.pending_decision(
                cid,
                "fictional-tool",
                json.dumps(
                    {
                        "operation": "reason",
                        "arguments": {"question": "What should I answer?"},
                    }
                ),
            )
            await service.local_context_worker.answer(service.call("owner", cid), pending)
            final = service.call("owner", cid)
            advice = final["reasoner_advice"][-1]
            assert advice["status"] == "ready" and "library_card" in advice["supporting_fact_names"]
            preparation = final["usage"]["reasoner_preparation"]
            handoff = final["usage"]["call_reasoner"]["last_handoff"]
            assert preparation["thread_id"] == handoff["thread_id"]
            assert handoff["input_mode"] == "delta"
            assert "supporting_quote" not in carrier.answers[-1]
            result = {
                "passed": True,
                "phone_calls": 0,
                "realtime_requests": 0,
                "connected_source_queries": 0,
                "preparation": preparation,
                "handoff": handoff,
                "voice_payload": carrier.answers[-1],
            }
            await service.end("owner", cid)
            output = directory / "result.json"
            output.write_text(json.dumps(result, indent=2) + "\n")
            output.chmod(0o600)
            print(json.dumps(result))
    finally:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 5)
        except TimeoutError:
            process.kill()
            await process.wait()
        if store:
            store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    asyncio.run(run(parser.parse_args().output.resolve()))
