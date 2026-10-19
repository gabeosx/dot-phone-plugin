import asyncio
import base64
import json
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import uvicorn
from pydantic import SecretStr
from twilio.request_validator import RequestValidator

pytest.importorskip("pipecat")
import websockets

from dots_phone.models import Start
from dots_phone.pipecat_realtime import SupervisedRealtimeService
from dots_phone.pipecat_voice import PipecatTelephony, SupervisedLiveService
from dots_phone.server import create_app
from tests.conftest import FakeProvider, FakeSender, subscribed


@pytest.mark.parametrize(
    ("engine", "shared", "silent_wait"),
    [
        ("live", False, False),
        ("realtime", False, False),
        ("realtime", True, False),
        ("realtime", True, True),
    ],
)
@pytest.mark.parametrize("prepared", [False, True])
async def test_signed_carrier_codec_mcp_decision_and_graceful_final_usage(
    settings, brief, monkeypatch, engine, prepared, shared, silent_wait, tmp_path
):
    updates, model_events, model_prompts, advertised_tools, response_requests = [], [], [], [], []
    if shared:
        from pipecat.processors.aggregators.llm_response_universal import LLMAssistantAggregator

        original_result = LLMAssistantAggregator._handle_function_call_result

        async def delayed_speaking_bookkeeping(aggregator, frame):
            # Reproduce a delayed SDK user-stop frame. The raw voice observer
            # is settled; local feedback must still produce a continuation.
            speaking = aggregator._user_speaking
            aggregator._user_speaking = True
            try:
                await original_result(aggregator, frame)
            finally:
                aggregator._user_speaking = speaking

        monkeypatch.setattr(
            LLMAssistantAggregator, "_handle_function_call_result", delayed_speaking_bookkeeping
        )

    silent_outputs = []

    async def model_socket(ws):
        delegated = False
        wait_sent = wait_settled = False
        sequence = 0
        response_count = 0

        async def send_rt(event):
            nonlocal sequence
            sequence += 1
            await ws.send(json.dumps({"event_id": f"event_{sequence}", **event}))

        async def rt_response(*, audio=False):
            nonlocal response_count
            response_count += 1
            response_id = f"response_{response_count}"
            response = {
                "id": response_id,
                "object": "realtime.response",
                "status": "in_progress",
                "status_details": None,
                "output": [],
            }
            await send_rt({"type": "response.created", "response": response})
            if audio:
                for _ in range(10):
                    await send_rt(
                        {
                            "type": "response.output_audio.delta",
                            "response_id": response_id,
                            "item_id": "voice_item",
                            "output_index": 0,
                            "content_index": 0,
                            "delta": base64.b64encode(b"\x00\x10" * 2400).decode(),
                        }
                    )
                    await asyncio.sleep(0.05)
            usage = {
                "total_tokens": 3,
                "input_tokens": 2,
                "output_tokens": 1,
                "input_token_details": {"text_tokens": 2, "audio_tokens": 0, "cached_tokens": 0},
                "output_token_details": {"text_tokens": 0, "audio_tokens": 1},
            }
            await send_rt(
                {
                    "type": "response.done",
                    "response": {**response, "status": "completed", "usage": usage},
                }
            )

        if engine == "realtime":
            await send_rt(
                {
                    "type": "session.created",
                    "session": {"id": "realtime_fixture", "model": "gpt-realtime-2.1"},
                }
            )
        async for raw in ws:
            event = json.loads(raw)
            model_events.append(event["type"])
            if engine == "realtime":
                if event["type"] == "session.update":
                    model_prompts.append(event["session"].get("instructions", ""))
                    advertised_tools.append([t["name"] for t in event["session"].get("tools", [])])
                    await send_rt(
                        {
                            "type": "session.updated",
                            "session": {
                                **event["session"],
                                "id": "realtime_fixture",
                                "model": "gpt-realtime-2.1",
                            },
                        }
                    )
                elif event["type"] == "response.create":
                    response_requests.append(event.get("response", {}))
                    await rt_response(audio=response_count == 0)
                    if shared and response_count == 1:
                        await send_rt(
                            {
                                "type": "conversation.item.added",
                                "previous_item_id": None,
                                "item": {
                                    "id": "invalid_report_item",
                                    "type": "function_call",
                                    "call_id": "invalid_report",
                                    "name": "finish_call",
                                    "arguments": "",
                                },
                            }
                        )
                        await send_rt(
                            {
                                "type": "response.function_call_arguments.done",
                                "response_id": "response_invalid_report",
                                "item_id": "invalid_report_item",
                                "output_index": 0,
                                "call_id": "invalid_report",
                                "arguments": json.dumps(
                                    {
                                        "outcome": "completed",
                                        "evidence": [
                                            {
                                                "criterion_id": "goal",
                                                "text": "A claimed result",
                                                "quote": "x" * 300,
                                            }
                                        ],
                                        "unresolved_questions": [],
                                    }
                                ),
                            }
                        )
                elif event["type"] == "input_audio_buffer.append" and not delegated:
                    if silent_wait and not wait_sent:
                        wait_sent = True
                        await send_rt(
                            {
                                "type": "conversation.item.added",
                                "previous_item_id": None,
                                "item": {
                                    "id": "wait_item",
                                    "type": "function_call",
                                    "call_id": "silent_wait",
                                    "name": "wait_for_recipient",
                                    "arguments": "",
                                },
                            }
                        )
                        await send_rt(
                            {
                                "type": "response.function_call_arguments.done",
                                "response_id": "wait_response",
                                "item_id": "wait_item",
                                "output_index": 0,
                                "call_id": "silent_wait",
                                "arguments": "{}",
                            }
                        )
                        continue
                    if silent_wait and not wait_settled:
                        continue
                    delegated = True
                    await send_rt(
                        {
                            "type": "conversation.item.input_audio_transcription.completed",
                            "item_id": "caller_item",
                            "content_index": 0,
                            "transcript": "Which date should I use?",
                        }
                    )
                    await send_rt(
                        {
                            "type": "conversation.item.added",
                            "previous_item_id": None,
                            "item": {
                                "id": "tool_item",
                                "type": "function_call",
                                "call_id": "delegation_fixture",
                                "name": "search_call_sources" if shared else "request_supervision",
                                "arguments": "",
                            },
                        }
                    )
                    await send_rt(
                        {
                            "type": "response.function_call_arguments.done",
                            "response_id": "response_tool",
                            "item_id": "tool_item",
                            "output_index": 0,
                            "call_id": "delegation_fixture",
                            "arguments": json.dumps(
                                {"query" if shared else "question": "Which date should I use?"}
                            ),
                        }
                    )
                elif (
                    event["type"] == "conversation.item.create"
                    and event["item"]["type"] == "function_call_output"
                ):
                    if event["item"]["call_id"] == "silent_wait":
                        # Settling a quiet tool is a wire operation, independent
                        # of asking for another response or a future context push.
                        assert not response_requests
                        silent_outputs.append(event["item"]["output"])
                        wait_settled = True
                    else:
                        updates.append(event["item"]["output"])
                    await send_rt(
                        {
                            "type": "conversation.item.added",
                            "previous_item_id": None,
                            "item": {
                                "id": "output_" + event["item"]["call_id"],
                                **event["item"],
                            },
                        }
                    )
                continue
            if event["type"] == "session.start":
                model_prompts.append(event["session"].get("instructions", ""))
                await ws.send(
                    json.dumps(
                        {
                            "type": "session.started",
                            "session": {"id": "live_fixture", "model": "gpt-live-1"},
                        }
                    )
                )
            elif event["type"] == "session.input_audio.append" and not delegated:
                delegated = True
                for message in [
                    {
                        "type": "session.input_transcript.delta",
                        "delta": "Which date should I use?",
                        "start_ms": 0,
                        "end_ms": 100,
                    },
                    {
                        "type": "session.delegation.created",
                        "delegation": {"id": "delegation_fixture", "target": "client"},
                    },
                ]:
                    await ws.send(json.dumps(message))
                for _ in range(10):
                    await ws.send(
                        json.dumps(
                            {
                                "type": "session.output_audio.delta",
                                "delta": base64.b64encode(b"\x00\x10" * 2400).decode(),
                            }
                        )
                    )
                    await asyncio.sleep(0.05)
            elif event["type"] == "session.commentary.append":
                updates.append(event["content"])
            elif event["type"] == "session.close":
                await ws.send(
                    json.dumps(
                        {
                            "type": "session.closed",
                            "reason": "close_requested",
                            "usage": {"seconds": 3.0},
                        }
                    )
                )

    async with websockets.serve(model_socket, "127.0.0.1", 0) as model:
        model_port = model.sockets[0].getsockname()[1]
        voice_class = SupervisedRealtimeService if engine == "realtime" else SupervisedLiveService
        original = voice_class.__init__

        def fixture_init(self, **kwargs):
            original(self, **kwargs, base_url=f"ws://127.0.0.1:{model_port}")

        monkeypatch.setattr(voice_class, "__init__", fixture_init)
        settings.pipecat_engine = engine
        settings.twilio_account = SecretStr("AC" + "2" * 32)
        settings.twilio_auth = SecretStr("fixture-carrier-token")
        settings.openai_key = SecretStr("offline-model-fixture")
        settings.phone_number = "+12125550101"
        settings.media_base_url = "https://media.example"
        app = create_app(settings, FakeProvider(), FakeSender())
        service = app.state.service
        remote = MagicMock()
        remote.calls.create.return_value = SimpleNamespace(sid="CA" + "1" * 32)
        remote.calls.return_value.fetch.return_value = SimpleNamespace(status="completed")
        service.provider = PipecatTelephony(settings, service, remote)
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False)
        )
        server_task = asyncio.create_task(server.serve())
        try:
            for _ in range(200):
                if server.started:
                    break
                await asyncio.sleep(0.01)
            assert server.started
            if shared:
                from dots_phone.call_task import RESULT_ACTION, SIMULATED_CONSTRAINT, SOURCE_ACTION
                from dots_phone.models import CompletionCriterion

                brief = brief.model_copy(
                    update={
                        "actions": [SOURCE_ACTION, RESULT_ACTION],
                        "constraints": [SIMULATED_CONSTRAINT],
                        "max_seconds": 180,
                        "completion_criteria": [
                            CompletionCriterion(
                                id="goal", description="Establish the requested information."
                            )
                        ],
                    }
                )
            if prepared:
                from tests.test_dot_preparation import request

                brief = request(brief)
            call_id = service.prepare("owner", brief)["call_id"]
            if shared:
                import time

                from dots_phone.local_gmail_worker import private_json
                from dots_phone.local_trip_mail_worker import LocalTripMailWorker

                directory = tmp_path / "source-worker"
                directory.mkdir(mode=0o700)
                call = service.call("owner", call_id)
                private_json(
                    directory / "registry.json",
                    {
                        "schema": 6,
                        "cases": [
                            {
                                "call_id": call_id,
                                "revision": 1,
                                "brief_hash": call["brief_hash"],
                                "provider": "call_sources",
                                "max_requests": 10,
                                "mail_scope": {},
                                "fixture_sources": [
                                    {
                                        "source_id": "record",
                                        "subject": "Date record",
                                        "text": "The date is 2026-11-06.",
                                    }
                                ],
                            }
                        ],
                    },
                )
                bridge = LocalTripMailWorker(service, directory)
                service.local_context_worker = bridge
                private_json(
                    directory / "worker-ready.json",
                    {
                        "schema": 1,
                        "serving": True,
                        "registry_hash": bridge.registry_hash,
                        "heartbeat_ms": time.time() * 1000,
                    },
                )

                async def externally_answered(*args):
                    await asyncio.Future()

                bridge.answer = AsyncMock(side_effect=externally_answered)
            await subscribed(service, call_id)
            authorization = service.approve("owner", call_id)
            settings.dry_run = False  # Mock carrier and local model only.
            service.start(
                "owner", Start(call_id=call_id, revision=1, authorization_id=authorization)
            )
            await service.tick()
            token = app.state.oauth.issue("fixture-client").access_token
            signature = RequestValidator(settings.twilio_auth.get_secret_value()).compute_signature(
                settings.media_origin + "/voice", {}
            )
            async with websockets.connect(
                f"ws://127.0.0.1:{port}/voice", additional_headers={"x-twilio-signature": signature}
            ) as carrier:
                for message in [
                    {"event": "connected"},
                    {
                        "event": "start",
                        "start": {
                            "accountSid": settings.twilio_account.get_secret_value(),
                            "callSid": "CA" + "1" * 32,
                            "streamSid": "MZ" + "3" * 32,
                            "customParameters": {"dots_call_id": call_id},
                        },
                    },
                ]:
                    await carrier.send(json.dumps(message))
                # A real carrier streams continuously; the 8->24k resampler
                # needs several packets before its initial filter emits audio.
                for _ in range(25):
                    await carrier.send(
                        json.dumps(
                            {
                                "event": "media",
                                "media": {"payload": base64.b64encode(b"\xff" * 160).decode()},
                            }
                        )
                    )
                    await asyncio.sleep(0.02)
                if not shared:
                    try:
                        rendered = json.loads(await asyncio.wait_for(carrier.recv(), timeout=5))
                    except TimeoutError:
                        pytest.fail(
                            f"No rendered carrier audio; local model events: {model_events}"
                        )
                    if engine == "live":
                        assert rendered["event"] == "clear"
                        assert rendered["streamSid"] == "MZ" + "3" * 32
                        rendered = json.loads(await asyncio.wait_for(carrier.recv(), timeout=5))
                    assert rendered["event"] == "media"
                for _ in range(200):
                    if service.get("owner", call_id)["pending"]:
                        break
                    await asyncio.sleep(0.01)
                decision = service.get("owner", call_id)["pending"]
                assert decision, {
                    "model_events": model_events,
                    "state": service.get("owner", call_id),
                }
                params = {
                    "call_id": call_id,
                    "request_id": decision["request_id"],
                    "revision": decision["revision"],
                    "answer": "The approved date is 2026-11-06.",
                    "fact_names": ["date"],
                }
                async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as caller:
                    response = await caller.post(
                        "/mcp",
                        headers={
                            "Authorization": "Bearer " + token,
                            "MCP-Protocol-Version": "2026-07-28",
                            "Accept": "application/json, text/event-stream",
                            "mcp-method": "tools/call",
                            "mcp-name": "submit_call_decision",
                        },
                        json={
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "tools/call",
                            "params": {
                                "name": "submit_call_decision",
                                "arguments": params,
                                "_meta": {
                                    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                                    "io.modelcontextprotocol/clientCapabilities": {},
                                },
                            },
                        },
                    )
                assert response.status_code == 200
                if shared:
                    from dots_phone.models import Decision

                    assert response.json()["result"].get("isError", False)
                    assert "assigned to the local subscription worker" in response.text
                    await service.decision("owner", Decision.model_validate(params))
                else:
                    assert not response.json()["result"].get("isError", False), response.text
                if shared:
                    for _ in range(4):
                        rendered = json.loads(await asyncio.wait_for(carrier.recv(), timeout=5))
                        if rendered["event"] == "media":
                            break
                    assert rendered["event"] == "media"
                    # A local schema rejection must resume this actual SDK
                    # pipeline, without another user turn to unstick it.
                    for _ in range(500):
                        if model_events.count("response.create") >= 2:
                            break
                        await asyncio.sleep(0.01)
                    assert model_events.count("response.create") >= 2
                    retry = response_requests[-1]
                    assert retry["tool_choice"] == "auto"
                    assert {t["name"] for t in retry["tools"]} >= {
                        "finish_call",
                        "search_call_sources",
                        "read_call_source",
                        "hang_up",
                    }
            for _ in range(200):
                call = service.store.get("call", call_id)
                if call["state"] in {"failed", "completed", "cancelled"}:
                    break
                await asyncio.sleep(0.01)
            assert call["state"] in {"failed", "completed", "cancelled"}
            # Actual pinned transport must pass both audio directions through
            # the encrypted recorder, not merely instantiate a recorder object.
            for _ in range(200):
                call = service.store.get("call", call_id)
                if call.get("recording", {}).get("state") == "complete":
                    break
                await asyncio.sleep(0.01)
            assert call["recording"]["state"] == "complete"
            assert call["recording"]["recipient_bytes"] == 25 * 160
            assert call["recording"]["assistant_bytes"] > 0
            from dots_phone.export_evidence import export

            evidence = export(settings, call_id, tmp_path / "exported")
            assert evidence["recording"]["chunks"] > 0
            assert (tmp_path / "exported" / "conversation.wav").stat().st_size > 44
            if engine == "live":
                assert call["usage"]["live_usage_confirmed"]
                assert call["usage"]["live_seconds"] == 3
            else:
                assert call["usage"]["realtime_usage_confirmed"]
                assert call["usage"]["voice_closed_confirmed"]
                assert len(call["usage"]["realtime_response_usage"]) == 2
            remote.calls.return_value.update.assert_called_with(status="completed")
        finally:
            server.should_exit = True
            await asyncio.wait_for(server_task, timeout=20)
    if silent_wait:
        assert [json.loads(value) for value in silent_outputs] == [{"listening": True}]
    assert any("2026-11-06" in text for text in updates)
    if shared:
        assert any(json.loads(text).get("result_recorded") is False for text in updates)
        assert any(
            set(names)
            == {
                "search_call_sources",
                "read_call_source",
                "finish_call",
                "hang_up",
                "wait_for_recipient",
            }
            for names in advertised_tools
        )
        assert all("can check in without" not in text for text in model_prompts)
    if prepared:
        assert any("west lobby" in prompt for prompt in model_prompts)
        assert any("source_sha256" in prompt for prompt in model_prompts)
