import asyncio
import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("pipecat")

from dots_phone.models import Decision
from dots_phone.pipecat_realtime import SupervisedRealtimeService
from dots_phone.pipecat_voice import PipecatTelephony
from dots_phone.service import Refused
from tests.conftest import started, subscribed


@pytest.mark.parametrize("explicit_disable", [False, True])
async def test_noise_filter_disable_preserves_explicit_null_only(service, brief, explicit_disable):
    from pipecat.services.openai.realtime import events

    cid = service.prepare("owner", brief)["call_id"]
    llm = SupervisedRealtimeService(supervisor=service, call_id=cid, api_key="offline-fixture")
    llm._websocket = SimpleNamespace(send=AsyncMock())
    audio_input = events.AudioInput(**({"noise_reduction": None} if explicit_disable else {}))
    await llm.send_client_event(
        events.SessionUpdateEvent(
            session=events.SessionProperties(audio=events.AudioConfiguration(input=audio_input))
        )
    )
    config = json.loads(llm._websocket.send.call_args.args[0])["session"]["audio"]["input"]
    assert config == ({"noise_reduction": None} if explicit_disable else {})


@pytest.mark.parametrize("policy", [None, "always_consult"])
async def test_autonomous_wire_requires_reasoning_and_old_advice_cannot_open_a_new_turn(
    service, brief, policy
):
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.services.openai.realtime import events

    from dots_phone.call_task import task_tools

    cid, _ = await started(service, brief)
    call = service.call("owner", cid)
    call["runtime_supervision"] = "local_reasoner"
    if policy is not None:
        call["reasoner_response_policy"] = policy
    service.save(call)
    llm = SupervisedRealtimeService(supervisor=service, call_id=cid, api_key="offline-fixture")
    llm.prepare_listen_first(
        LLMContext([], tools=task_tools(llm, reasoner=True, source_tools=False))
    )
    llm._websocket = SimpleNamespace(send=AsyncMock())
    await llm.send_client_event(events.SessionUpdateEvent(session=events.SessionProperties()))
    session = json.loads(llm._websocket.send.call_args.args[0])["session"]
    assert session["tool_choice"] == "required"
    assert [t["name"] for t in session["tools"]] == ["consult_call_reasoner"]
    await llm.send_client_event(events.ResponseCreateEvent())
    assert (
        json.loads(llm._websocket.send.call_args.args[0])["response"]["tool_choice"] == "required"
    )

    llm.check_decision = lambda *args: None
    llm._send_tool_result = AsyncMock()
    llm.source_continuation_task = SimpleNamespace(done=lambda: False)
    llm.reasoner_tool_turns["advice"] = llm.reasoner_turn
    call["pending"] = {"request_id": "fixture"}
    await llm.deliver_source(
        call,
        "advice",
        json.dumps({"status": "ready", "answer": "verified"}),
        SimpleNamespace(result_callback=AsyncMock()),
        asyncio.get_running_loop().create_future(),
    )
    await llm.send_client_event(events.ResponseCreateEvent())
    response = json.loads(llm._websocket.send.call_args.args[0])["response"]
    assert response["tool_choice"] == "auto"
    assert {"finish_call", "wait_for_recipient"} <= {t["name"] for t in response["tools"]}

    await llm.observe({"type": "input_audio_buffer.speech_started"})
    # An old result delivered after an interruption still cannot authorize speech.
    llm.reasoner_tool_turns["late"] = llm.reasoner_turn - 1
    await llm.deliver_source(
        call,
        "late",
        json.dumps({"status": "ready", "answer": "old"}),
        SimpleNamespace(result_callback=AsyncMock()),
        asyncio.get_running_loop().create_future(),
    )
    await llm.send_client_event(events.ResponseCreateEvent())
    response = json.loads(llm._websocket.send.call_args.args[0])["response"]
    assert response["tool_choice"] == "required"
    assert [t["name"] for t in response["tools"]] == ["consult_call_reasoner"]


async def test_preconsultation_audio_and_transcript_are_never_forwarded(
    service, brief, monkeypatch
):
    from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService

    cid, _ = await started(service, brief)
    call = service.call("owner", cid)
    call["runtime_supervision"] = "local_reasoner"
    service.save(call)
    llm = SupervisedRealtimeService(supervisor=service, call_id=cid, api_key="offline-fixture")
    forwarded = AsyncMock()
    monkeypatch.setattr(OpenAIRealtimeLLMService, "_handle_evt_audio_delta", forwarded)
    await llm.observe({"type": "response.created", "response": {"id": "before"}})
    delta = base64.b64encode(b"\0\0" * 80).decode()
    await llm._handle_evt_audio_delta(
        SimpleNamespace(response_id="before", item_id="speech", delta=delta)
    )
    await llm.observe(
        {
            "type": "response.output_audio_transcript.done",
            "response_id": "before",
            "item_id": "speech",
            "transcript": "I refuse before consulting.",
        }
    )
    forwarded.assert_not_awaited()
    assert llm.dialog == []
    assert llm.speech_delivery["audio_bytes_before_reasoning"] == 160
    llm.reasoner_consulted_turn = llm.reasoner_turn
    await llm.observe({"type": "response.created", "response": {"id": "after"}})
    await llm._handle_evt_audio_delta(
        SimpleNamespace(response_id="after", item_id="approved", delta=delta)
    )
    forwarded.assert_awaited_once()


async def test_direct_policy_allows_ordinary_audio_and_keeps_codex_available_each_turn(
    service, brief, monkeypatch
):
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.services.openai.realtime import events
    from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService

    from dots_phone.call_task import task_tools

    cid, _ = await started(service, brief)
    call = service.call("owner", cid)
    call.update(runtime_supervision="local_reasoner", reasoner_response_policy="direct_with_codex")
    service.save(call)
    service.local_context_worker = SimpleNamespace(scope=lambda call: {}, directory=None)
    llm = SupervisedRealtimeService(supervisor=service, call_id=cid, api_key="offline-fixture")
    llm.prepare_listen_first(
        LLMContext([], tools=task_tools(llm, reasoner=True, source_tools=False))
    )
    llm._websocket = SimpleNamespace(send=AsyncMock())
    forwarded = AsyncMock()
    monkeypatch.setattr(OpenAIRealtimeLLMService, "_handle_evt_audio_delta", forwarded)
    await llm.send_client_event(events.SessionUpdateEvent(session=events.SessionProperties()))
    session = json.loads(llm._websocket.send.call_args.args[0])["session"]
    assert session["tool_choice"] == "auto"
    assert {"consult_call_reasoner", "finish_call", "wait_for_recipient"} <= {
        tool["name"] for tool in session["tools"]
    }
    for index in range(2):
        await llm.observe({"type": "input_audio_buffer.speech_started"})
        await llm.send_client_event(events.ResponseCreateEvent())
        response = json.loads(llm._websocket.send.call_args.args[0])["response"]
        assert response["tool_choice"] == "auto" and response["tools"] == session["tools"]
        identity = f"ordinary-{index}"
        await llm.observe({"type": "response.created", "response": {"id": identity}})
        await llm._handle_evt_audio_delta(
            SimpleNamespace(
                response_id=identity,
                item_id=identity,
                delta=base64.b64encode(b"\0\0" * 80).decode(),
            )
        )
        await llm.observe(
            {
                "type": "response.output_audio_transcript.done",
                "response_id": identity,
                "item_id": identity,
                "transcript": "November sixth.",
            }
        )
    assert forwarded.await_count == 2
    assert llm.reasoner_consulted_turn == -1
    assert not llm.reasoner_blocked_responses
    assert [entry["text"] for entry in llm.dialog] == ["November sixth."] * 2


@pytest.mark.parametrize(
    "change", ["revoked", "document", "expiry", "revision", "cancelled", "grant_mid_speech"]
)
async def test_direct_response_rechecks_scope_and_suppresses_changed_output(
    service, brief, monkeypatch, change
):
    from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService

    cid, _ = await started(service, brief)
    call = service.call("owner", cid)
    call.update(runtime_supervision="local_reasoner", reasoner_response_policy="direct_with_codex")
    service.save(call)
    bridge = SimpleNamespace(scope=lambda call: {}, directory=None)
    service.local_context_worker = bridge
    llm = SupervisedRealtimeService(supervisor=service, call_id=cid, api_key="offline-fixture")
    forwarded = AsyncMock()
    monkeypatch.setattr(OpenAIRealtimeLLMService, "_handle_evt_audio_delta", forwarded)
    await llm.observe({"type": "response.created", "response": {"id": "ordinary"}})
    if change == "grant_mid_speech":
        await llm._handle_evt_audio_delta(
            SimpleNamespace(
                response_id="ordinary",
                item_id="ordinary",
                delta=base64.b64encode(b"\0\0" * 80).decode(),
            )
        )
        forwarded.assert_awaited_once()
        forwarded.reset_mock()
        service.store.delete("approval", call["started_authorization"])
    elif change == "revoked":

        def revoked(_):
            raise Refused("grant revoked")

        bridge.scope = revoked
    elif change == "expiry":
        llm.direct_response_scopes["ordinary"]["expires"] = 0
    else:
        fresh = service.call("owner", cid)
        if change == "document":
            fresh["prep_document"]["markdown"] += "Changed without approval."
        elif change == "revision":
            fresh["revision"] += 1
        else:
            fresh["state"] = "cancelled"
        service.save(fresh)
    await llm._handle_evt_audio_delta(
        SimpleNamespace(
            response_id="ordinary",
            item_id="ordinary",
            delta=base64.b64encode(b"\0\0" * 80).decode(),
        )
    )
    forwarded.assert_not_awaited()
    assert "ordinary" in llm.reasoner_blocked_responses
    if llm.stop_task:
        await llm.stop_task


async def test_separate_transport_failures_can_recover_without_a_call_lifetime_count_cap(
    service, brief
):
    from websockets.exceptions import ConnectionClosedError

    cid, _ = await started(service, brief)
    llm = SupervisedRealtimeService(supervisor=service, call_id=cid, api_key="offline-fixture")
    llm._settings.system_instruction = "Approved read-only scope."
    llm.dialog = [{"role": "user", "text": "Please hold for the representative."}]
    original = SimpleNamespace(close=AsyncMock())
    llm._websocket = original
    llm.clear_playback = AsyncMock()

    async def reconnect():
        llm._websocket = SimpleNamespace(send=AsyncMock())
        llm.ready.set()

    llm._connect = AsyncMock(side_effect=reconnect)
    llm.voice_failure("receive_exception", ConnectionClosedError(None, None))
    llm.voice_failure("input_dispatch", ConnectionClosedError(None, None))
    await llm.reconnect_task
    assert service.call("owner", cid)["state"] == "active"
    assert len(service.provider.dials) == 1 and not service.provider.stops
    llm._connect.assert_awaited_once()
    llm._websocket.send.assert_not_awaited()
    assert llm._settings.system_instruction.startswith("Approved read-only scope.")
    assert "untrusted evidence" in llm._settings.system_instruction
    assert "Please hold" in llm._settings.system_instruction
    assert (
        service.call("owner", cid)["usage"]["realtime_transport_recoveries"][0]["state"]
        == "recovered"
    )
    llm._websocket.close = AsyncMock()
    llm.voice_failure("receive_exception", ConnectionClosedError(None, None))
    llm.voice_failure("input_dispatch", ConnectionClosedError(None, None))
    await llm.reconnect_task
    assert llm._connect.await_count == llm.reconnect_attempts == 2
    assert len(service.provider.dials) == 1 and not service.provider.stops
    assert service.call("owner", cid)["state"] == "active"
    assert [
        r["state"] for r in service.call("owner", cid)["usage"]["realtime_transport_recoveries"]
    ] == ["recovered", "recovered"]
    assert llm._settings.system_instruction.count("The model transport was replaced") == 1


async def test_transport_recovery_failure_is_bounded_and_private(service, brief):
    from websockets.exceptions import ConnectionClosedError

    cid, _ = await started(service, brief)
    llm = SupervisedRealtimeService(supervisor=service, call_id=cid, api_key="offline-fixture")
    llm._settings.system_instruction = "Approved scope."
    llm._websocket = SimpleNamespace(close=AsyncMock())
    llm._connect = AsyncMock(side_effect=RuntimeError("private-provider-message"))
    llm.voice_failure("receive_exception", ConnectionClosedError(None, None))
    await llm.reconnect_task
    await llm.stop_task
    call = service.call("owner", cid)
    assert call["state"] == "failed"
    assert call["usage"]["realtime_transport_recoveries"][0]["state"] == "failed"
    assert "private-provider-message" not in json.dumps(call)
    assert len(service.provider.stops) == 1


async def test_real_sdk_reconnects_after_socket_abort_and_accepts_carrier_audio(service, brief):
    import websockets
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.services.llm_service import FunctionCallParams
    from pipecat.services.openai.realtime import events

    async def wait_for_recipient(params: FunctionCallParams):
        pass

    cid, _ = await started(service, brief)
    sockets, prompts, received = [], [], []

    async def model_socket(ws):
        sockets.append(ws)
        session_id = f"session_{len(sockets)}"
        await ws.send(
            json.dumps(
                {
                    "type": "session.created",
                    "event_id": session_id,
                    "session": {"id": session_id, "model": "gpt-realtime-2.1"},
                }
            )
        )
        try:
            async for raw in ws:
                event = json.loads(raw)
                received.append(event["type"])
                if event["type"] == "session.update":
                    prompts.append(event["session"]["instructions"])
                    await ws.send(
                        json.dumps(
                            {
                                "type": "session.updated",
                                "event_id": session_id + "_updated",
                                "session": {
                                    **event["session"],
                                    "id": session_id,
                                    "model": "gpt-realtime-2.1",
                                },
                            }
                        )
                    )
        except websockets.ConnectionClosed:
            pass

    async with websockets.serve(model_socket, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        llm = SupervisedRealtimeService(
            supervisor=service,
            call_id=cid,
            api_key="offline-fixture",
            base_url=f"ws://127.0.0.1:{port}",
            settings=SupervisedRealtimeService.Settings(
                system_instruction="Approved scope.", session_properties=events.SessionProperties()
            ),
        )
        llm.prepare_listen_first(LLMContext([], tools=[wait_for_recipient]))
        llm.dialog = [{"role": "user", "text": "The representative will be with you shortly."}]
        llm.create_task = lambda coroutine, *args, **kwargs: asyncio.create_task(coroutine)

        async def cancel(task, **kwargs):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        llm.cancel_task = cancel
        try:
            await llm._connect()
            await asyncio.wait_for(llm.ready.wait(), 2)
            sockets[0].transport.abort()
            for _ in range(300):
                recovery = service.call("owner", cid)["usage"].get(
                    "realtime_transport_recoveries", []
                )
                if recovery and recovery[0]["state"] == "recovered":
                    break
                await asyncio.sleep(0.01)
            assert recovery[0]["state"] == "recovered"
            assert not service.call("owner", cid)["usage"].get("voice_closed_confirmed")
            assert len(sockets) == len(llm.session_ids) == 2
            assert "untrusted evidence" in prompts[-1]
            await llm.send_client_event(
                events.InputAudioBufferAppendEvent(
                    audio=base64.b64encode(b"\x00\x10" * 160).decode()
                )
            )
            for _ in range(50):
                if "input_audio_buffer.append" in received:
                    break
                await asyncio.sleep(0.01)
            assert "input_audio_buffer.append" in received
            assert len(service.provider.dials) == 1 and not service.provider.stops
            assert service.call("owner", cid)["state"] == "active"
        finally:
            llm.stop_requested = llm.closing = True
            await llm._disconnect()


async def test_pending_source_lookup_does_not_replay_tool_on_reconnection(service, brief):
    from websockets.exceptions import ConnectionClosedError

    cid, llm, _, lookup, _ = await pending_voice(service, brief)
    try:
        llm.voice_failure("receive_exception", ConnectionClosedError(None, None))
        assert llm.reconnect_task is None
        await llm.stop_task
        assert service.call("owner", cid)["state"] == "failed"
    finally:
        lookup.cancel()
        await asyncio.gather(lookup, return_exceptions=True)


@pytest.mark.parametrize("close_code", [1000, 1001, 1006, 1011])
async def test_observed_socket_close_allows_cleanup_without_claiming_final_usage(
    service, brief, monkeypatch, close_code
):
    from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService

    call_id, _ = await started(service, brief)
    await subscribed(service, call_id)
    llm = SupervisedRealtimeService(supervisor=service, call_id=call_id, api_key="offline-fixture")
    llm._websocket = SimpleNamespace(close_code=close_code)
    monkeypatch.setattr(OpenAIRealtimeLLMService, "_disconnect", AsyncMock())
    await service.end("owner", call_id, "voice_path_failed")
    await llm._disconnect()
    call = service.call("owner", call_id)
    assert llm.closed.is_set()
    assert call["usage"]["voice_closed_confirmed"] is True
    assert call["usage"]["realtime_usage_confirmed"] is False
    assert call["usage"]["billing_confirmed"] is False
    assert call["state"] == "failed"
    assert service.store.db.execute("SELECT count(*) FROM deliveries").fetchone()[0] == 2


async def pending_voice(service, brief, *, source=False):
    call_id, _ = await started(service, brief)
    llm = SupervisedRealtimeService(supervisor=service, call_id=call_id, api_key="offline-fixture")
    llm._websocket = SimpleNamespace(send=AsyncMock())
    llm._truncate_current_audio_response = AsyncMock()
    llm._create_response = AsyncMock()
    llm.clear_playback = AsyncMock()
    params = SimpleNamespace(tool_call_id="tool_fixture", result_callback=AsyncMock())
    task = asyncio.create_task(
        llm.request_supervision(
            params, "Which approved date should I use?", defer_source_speech=source
        )
    )
    await asyncio.sleep(0)
    pending = service.get("owner", call_id)["pending"]
    return call_id, llm, params, task, pending


async def test_result_waits_for_new_caller_turn_and_audio_playback(service, brief):
    cid, llm, params, lookup, _ = await pending_voice(service, brief)
    await llm.observe({"type": "input_audio_buffer.speech_started"})
    call = service.call("owner", cid)
    delivery = asyncio.create_task(
        llm.deliver_decision(call, params.tool_call_id, "Verified source.")
    )
    await asyncio.sleep(0.03)
    llm._websocket.send.assert_not_awaited()
    llm._current_audio_response = object()
    await llm.observe({"type": "input_audio_buffer.speech_stopped"})
    await asyncio.sleep(0.12)
    llm._websocket.send.assert_not_awaited()
    llm._current_audio_response = None
    await asyncio.wait_for(delivery, 1)
    await lookup
    llm._websocket.send.assert_awaited_once()
    llm._create_response.assert_awaited_once()


async def test_second_unknown_queues_without_displacing_current_decision(service, brief):
    cid, llm, first_params, first_task, first = await pending_voice(service, brief)
    service.settings.pipecat_engine = "realtime"
    provider = PipecatTelephony(service.settings, service)
    provider.sessions["CA" + "1" * 32] = SimpleNamespace(llm=llm)
    service.provider = provider
    second_params = SimpleNamespace(tool_call_id="second_tool", result_callback=AsyncMock())
    second_task = asyncio.create_task(llm.request_supervision(second_params, "Another unknown"))
    await asyncio.sleep(0.03)
    assert service.get("owner", cid)["pending"]["request_id"] == first["request_id"]
    assert "second_tool" not in llm.decisions
    await service.decision(
        "owner",
        Decision(
            call_id=cid,
            request_id=first["request_id"],
            revision=1,
            answer="Use 2026-11-06.",
            fact_names=["date"],
        ),
    )
    await first_task
    for _ in range(100):
        second = service.get("owner", cid)["pending"]
        if second:
            break
        await asyncio.sleep(0.01)
    assert second["request_id"] != first["request_id"]
    await service.decision(
        "owner",
        Decision(
            call_id=cid,
            request_id=second["request_id"],
            revision=1,
            answer="Use 2026-11-06.",
            fact_names=["date"],
        ),
    )
    await second_task
    first_params.result_callback.assert_awaited_once()
    second_params.result_callback.assert_awaited_once()
    assert not llm.lookup_lock.locked()


async def test_prepared_packet_presence_is_checked_on_accepted_prompt_without_retaining_it(
    service, brief
):
    from tests.test_dot_preparation import request

    service.settings.trace_realtime_stages = True
    cid = service.prepare("owner", request(brief))["call_id"]
    llm = SupervisedRealtimeService(supervisor=service, call_id=cid, api_key="offline-fixture")
    call = service.call("owner", cid)
    hashes = [s["source_sha256"] for s in call["prepared_evidence"]["sources"]]
    for instructions, included in [("Missing packet", False), ("Private text " + hashes[0], True)]:
        await llm.observe({"type": "session.updated", "session": {"instructions": instructions}})
        metadata = service.call("owner", cid)["usage"]["prepared_evidence_transmission"]
        assert metadata["source_hashes_present"] is included
        assert metadata["source_count"] == 1
        assert instructions not in json.dumps(metadata)


async def test_actual_accepted_voice_configuration_is_auditable_without_private_contents(
    service, brief
):
    import hashlib

    cid = service.prepare("owner", brief)["call_id"]
    llm = SupervisedRealtimeService(supervisor=service, call_id=cid, api_key="offline-fixture")
    service.settings.trace_realtime_stages = False
    prompt = "Private briefing and source details"
    await llm.observe(
        {
            "type": "session.updated",
            "session": {
                "instructions": prompt,
                "tools": [
                    {"name": "search_call_sources", "parameters": {"private": "not retained"}},
                    {"name": "read_call_source"},
                ],
                "reasoning": {"effort": "low"},
            },
        }
    )
    recorded = service.call("owner", cid)["usage"]["accepted_voice_configuration"]
    assert recorded == {
        "tool_names": ["read_call_source", "search_call_sources"],
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "configured_prompt_sha256": None,
        "prompt_matches_configuration": False,
        "reasoning_effort": "low",
        "tool_choice": None,
    }
    assert prompt not in json.dumps(recorded) and "not retained" not in json.dumps(recorded)
    await llm.observe({"type": "session.updated", "session": {"tools": None, "instructions": None}})
    recorded = service.call("owner", cid)["usage"]["accepted_voice_configuration"]
    assert recorded["tool_names"] == [] and recorded["prompt_sha256"] is None
    llm.append_system_instruction(prompt)
    await llm.observe({"type": "session.updated", "session": {"instructions": prompt}})
    recorded = service.call("owner", cid)["usage"]["accepted_voice_configuration"]
    assert recorded["prompt_matches_configuration"] is True
    assert recorded["configured_prompt_sha256"] == recorded["prompt_sha256"]


async def test_listen_first_has_callable_tools_and_no_unsolicited_response(service, brief):
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.services.llm_service import FunctionCallParams
    from pipecat.services.openai.realtime import events

    cid, _ = await started(service, brief)
    llm = SupervisedRealtimeService(
        supervisor=service,
        call_id=cid,
        api_key="offline-fixture",
        settings=SupervisedRealtimeService.Settings(
            system_instruction="Approved brief. Listen before responding.",
            session_properties=events.SessionProperties(),
        ),
    )

    async def press_phone_key(params: FunctionCallParams, digit: str):
        """Choose the heard menu digit."""
        await params.result_callback({"accepted": True})

    context = LLMContext([], tools=[press_phone_key])
    llm._create_response = AsyncMock()
    llm.prepare_listen_first(context)
    assert llm._context is context and "press_phone_key" in llm._functions
    assert llm._settings.session_properties.tools is context.tools
    assert not llm._llm_needs_conversation_setup
    llm._create_response.assert_not_called()
    with pytest.raises(RuntimeError):
        llm.prepare_listen_first(context)


@pytest.mark.parametrize(
    ("failure", "phase"),
    [
        (Refused("request outside local Gmail scope"), "local_lookup_question_rejected"),
        (PermissionError("private-path-fixture"), "local_lookup_io_failure"),
        (RuntimeError("private-provider-fixture"), "local_lookup_runtime_failure"),
    ],
)
async def test_local_lookup_failure_is_observable_without_private_error_details(
    service, brief, settings, failure, phase
):
    settings.trace_realtime_stages = True
    service.local_gmail_worker = SimpleNamespace(
        registered=lambda _: True,
        require_ready=lambda _: None,
        answer=AsyncMock(side_effect=failure),
    )
    _, llm, params, task, _ = await pending_voice(service, brief)
    with pytest.raises(Refused, match="local subscription lookup failed"):
        await task
    assert [e["phase"] for e in llm.stage_events] == [
        "decision_request_created",
        "local_lookup_started",
        phase,
    ]
    assert str(failure) not in json.dumps(llm.stage_events)
    params.result_callback.assert_not_awaited()


async def test_authenticated_decision_reaches_realtime_before_context(service, brief, settings):
    call_id, llm, params, task, pending = await pending_voice(service, brief)
    settings.pipecat_engine = "realtime"
    provider = PipecatTelephony(settings, service)
    provider.sessions["CA" + "1" * 32] = SimpleNamespace(llm=llm)
    service.provider = provider

    async def context_result(result):
        assert params.tool_call_id in llm._completed_tool_calls
        assert llm._websocket.send.await_count == 1
        assert result["authorization_expanded"] is False

    params.result_callback.side_effect = context_result
    await service.decision(
        "owner",
        Decision(
            call_id=call_id,
            request_id=pending["request_id"],
            revision=1,
            answer="Use the approved date 2026-11-06.",
            fact_names=["date"],
        ),
    )
    await task
    wire = json.loads(llm._websocket.send.call_args.args[0])
    assert wire["type"] == "conversation.item.create"
    assert wire["item"]["type"] == "function_call_output"
    assert wire["item"]["call_id"] == "tool_fixture"
    assert "2026-11-06" in wire["item"]["output"]
    llm.clear_playback.assert_awaited_once_with("verified_result")
    llm._create_response.assert_awaited_once()
    assert service.get("owner", call_id)["state"] == "active"


async def test_realtime_send_failure_preserves_pending_decision(service, brief):
    call_id, llm, params, task, _ = await pending_voice(service, brief)
    llm._websocket.send.side_effect = ConnectionError("synthetic failure")
    try:
        with pytest.raises(ConnectionError):
            await llm.deliver_decision(
                service.call("owner", call_id), "tool_fixture", "bounded answer"
            )
        assert service.get("owner", call_id)["state"] == "awaiting_decision"
        assert "tool_fixture" not in llm._completed_tool_calls
        params.result_callback.assert_not_awaited()
        llm._create_response.assert_not_awaited()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("recorded", [True, False])
async def test_final_report_closes_without_an_extra_model_response_but_rejections_can_retry(
    service, brief, recorded
):
    _, llm, params, task, _ = await pending_voice(service, brief)
    llm.finalizing_calls.add("tool_fixture")
    llm.retry_final_report = AsyncMock()
    try:
        call = service.call("owner", llm.call_id)
        await llm.deliver_decision(call, "tool_fixture", json.dumps({"result_recorded": recorded}))
        await task
        if recorded:
            llm._create_response.assert_not_awaited()
            assert params.result_callback.call_args.kwargs["properties"].run_llm is False
        else:
            llm._create_response.assert_not_awaited()
            llm.retry_final_report.assert_awaited_once()
            assert params.result_callback.call_args.kwargs["properties"].run_llm is False
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_realtime_rechecks_revision_after_waiting_for_response(service, brief):
    call_id, llm, params, task, _ = await pending_voice(service, brief)
    before = service.call("owner", call_id)
    fresh = service.call("owner", call_id)
    fresh.update(revision=2, state="active", pending=None)
    service.save(fresh)
    try:
        with pytest.raises(Refused, match="stale"):
            await llm.deliver_decision(before, "tool_fixture", "obsolete answer")
        llm._websocket.send.assert_not_awaited()
        params.result_callback.assert_not_awaited()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_stage_timings_correlate_voiced_answer_and_do_not_retain_content(
    service, brief, settings
):
    settings.trace_realtime_stages = True
    call_id, llm, params, task, pending = await pending_voice(service, brief)
    await llm.deliver_decision(
        service.call("owner", call_id), "tool_fixture", "private-answer-fixture"
    )
    await task
    await llm.observe({"type": "response.created", "response": {"id": "answer"}})
    for audio in [b"\0\0" * 20, b"\0\x10" * 20, b"\0\x10" * 20]:
        await llm.observe(
            {
                "type": "response.output_audio.delta",
                "response_id": "answer",
                "delta": base64.b64encode(audio).decode(),
            }
        )
    usage = service.call("owner", call_id)["usage"]
    events = usage["realtime_stage_timings"]
    assert [e["phase"] for e in events] == [
        "decision_request_created",
        "delivery_received",
        "delivery_lock_acquired",
        "response_wait_finished",
        "playback_clear_finished",
        "tool_output_transmitted",
        "answer_response_requested",
        "first_answer_audio_received",
    ]
    assert all(e["request_id"] == pending["request_id"] for e in events)
    assert all(set(e) == {"phase", "request_id", "at_ms"} for e in events)
    assert "private-answer-fixture" not in json.dumps(usage)
    assert "Which approved date" not in json.dumps(usage)
    for _ in range(40):
        llm.stage("response_wait_finished", pending["request_id"])
    assert len(service.call("owner", call_id)["usage"]["realtime_stage_timings"]) == 32


async def test_stage_timings_are_absent_by_default(service, brief):
    call_id, llm, _, task, _ = await pending_voice(service, brief)
    llm.stage("delivery_received", None)
    assert "realtime_stage_timings" not in service.call("owner", call_id)["usage"]
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("change", ["revision", "expiry"])
async def test_realtime_rechecks_scope_after_playback_clear(service, brief, change):
    call_id, llm, params, task, _ = await pending_voice(service, brief)
    before = service.call("owner", call_id)

    async def change_scope(reason):
        fresh = service.call("owner", call_id)
        if change == "revision":
            fresh["revision"] += 1
        else:
            fresh["pending"]["expires"] = 0
        service.save(fresh)

    llm.clear_playback.side_effect = change_scope
    try:
        with pytest.raises(Refused, match="stale"):
            await llm.deliver_decision(before, "tool_fixture", "obsolete answer")
        llm._websocket.send.assert_not_awaited()
        params.result_callback.assert_not_awaited()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_realtime_continuation_retry_does_not_repeat_tool_output(service, brief):
    call_id, llm, params, task, _ = await pending_voice(service, brief)
    llm._create_response.side_effect = [ConnectionError("synthetic continuation failure"), None]
    call = service.call("owner", call_id)
    with pytest.raises(ConnectionError):
        await llm.deliver_decision(call, "tool_fixture", "approved answer")
    assert service.get("owner", call_id)["pending"]
    await llm.deliver_decision(call, "tool_fixture", "approved answer")
    await task
    assert llm._websocket.send.await_count == 1
    assert params.result_callback.await_count == 1
    assert llm._create_response.await_count == 2


async def test_realtime_usage_requires_cancelled_responses_too(service, brief):
    call_id, _ = await started(service, brief)
    llm = SupervisedRealtimeService(supervisor=service, call_id=call_id, api_key="offline-fixture")
    for response_id in ["story", "answer"]:
        await llm.observe({"type": "response.created", "response": {"id": response_id}})
    await llm.observe(
        {"type": "response.done", "response": {"id": "answer", "usage": {"total_tokens": 5}}}
    )
    assert not service.call("owner", call_id)["usage"]["realtime_usage_confirmed"]
    await llm.observe(
        {
            "type": "response.done",
            "response": {"id": "story", "status": "cancelled", "usage": {"total_tokens": 2}},
        }
    )
    assert service.call("owner", call_id)["usage"]["realtime_usage_confirmed"]
    assert len(service.call("owner", call_id)["usage"]["realtime_response_usage"]) == 2


@pytest.mark.parametrize("code", ["insufficient_quota", "server_error"])
async def test_failed_response_retains_cause_and_ends_unresolved_without_idle_wait(
    service, brief, code
):
    call_id, _ = await started(service, brief)
    llm = SupervisedRealtimeService(supervisor=service, call_id=call_id, api_key="offline-fixture")
    await llm.observe({"type": "response.created", "response": {"id": "failed_response"}})
    await llm.observe(
        {
            "type": "response.done",
            "response": {
                "id": "failed_response",
                "status": "failed",
                "status_details": {
                    "error": {"type": code, "code": code, "message": "private-provider-message"}
                },
                "usage": {"total_tokens": 0, "input_tokens": 0, "output_tokens": 0},
            },
        }
    )
    await asyncio.wait_for(llm.stop_task, 1)
    result = service.result("owner", call_id)
    assert result["state"] == "failed" and result["outcome"] == "unresolved"
    assert result["termination_reason"] == "voice_response_failed"
    assert result["usage"]["realtime_response_failure"]["code"] == code
    assert "private-provider-message" not in json.dumps(service.call("owner", call_id))
    assert not llm.active_responses


async def test_realtime_observed_transcript_is_transient_when_retention_is_disabled(service, brief):
    service.settings.retain_transcript = False
    call_id, _ = await started(service, brief)
    llm = SupervisedRealtimeService(supervisor=service, call_id=call_id, api_key="offline-fixture")
    event = {
        "type": "conversation.item.input_audio_transcription.completed",
        "item_id": "input",
        "transcript": "Which date?",
    }
    await llm.observe(event)
    await llm.observe(event)
    assert service.transcripts[call_id] == [{"role": "user", "text": "Which date?"}]
    assert not service.call("owner", call_id).get("transcript")


async def test_live_and_archived_realtime_history_keep_earlier_turns(service, brief):
    call_id, _ = await started(service, brief)
    llm = SupervisedRealtimeService(supervisor=service, call_id=call_id, api_key="offline-fixture")
    for i in range(45):
        event = {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": f"input_{i}",
            "transcript": f"Observed turn {i}",
        }
        await llm.observe(event)
        await llm.observe(event)
    assert len(llm.dialog) == len(service.transcripts[call_id]) == 45
    assert llm.dialog[0]["text"] == service.transcripts[call_id][0]["text"] == "Observed turn 0"
    archived = service.call("owner", call_id)["transcript"]
    assert len(archived) == 45
    assert archived[0]["text"] == "Observed turn 0"
    assert archived[-1]["text"] == "Observed turn 44"
    service.settings.retain_transcript = False  # Retrieval follows per-call capture policy.
    await service.end("owner", call_id, "owner_cancelled")
    assert len(service.result("owner", call_id)["transcript"]) == 45


def test_pipecat_engine_is_explicit_and_validated(settings, monkeypatch):
    from dots_phone.config import Settings

    monkeypatch.setenv("DOTS_PHONE_PROFILE", "evaluation")
    monkeypatch.setenv("DOTS_PHONE_PIPECAT_ENGINE", "realtime")
    assert Settings.from_env().pipecat_engine == "realtime"
    monkeypatch.setenv("DOTS_PHONE_PIPECAT_REALTIME_MODEL", "gpt-realtime-2.1-mini")
    monkeypatch.setenv("DOTS_PHONE_LOCAL_GMAIL_WORKER_DIRECTORY", "")
    configured = Settings.from_env()
    assert configured.pipecat_realtime_model == "gpt-realtime-2.1-mini"
    assert configured.local_gmail_worker_directory is None
    with pytest.raises(ValueError, match="unknown Pipecat engine"):
        settings.model_copy(update={"pipecat_engine": "unknown"}).validate_profile()
    with pytest.raises(ValueError, match="unknown Pipecat Realtime model"):
        settings.model_copy(update={"pipecat_realtime_model": "unknown"}).validate_profile()


async def test_story_interruption_does_not_authorize_model_hangup(service, brief):
    call_id, _ = await started(service, brief)
    llm = SupervisedRealtimeService(supervisor=service, call_id=call_id, api_key="offline-fixture")
    service.end = AsyncMock()
    await llm.observe(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "interrupt",
            "transcript": "Stop the story and look up the museum closing time.",
        }
    )
    assert not llm.request_stop()
    assert not llm.request_stop("Stop the story and look up the museum closing time.")
    await asyncio.sleep(0)
    service.end.assert_not_awaited()
    assert service.get("owner", call_id)["state"] == "active"
    await llm.observe(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "hangup",
            "transcript": "Thank you. End the call now.",
        }
    )
    await llm.stop_task
    service.end.assert_awaited_once_with("owner", call_id, "recipient_stop")


@pytest.mark.parametrize(
    "prompt",
    [
        "If that is all you need, please hang up to hear that again say repeat "
        "or say main menu for more options.",
        "You can say repeat that or to do something else. Say main menu or if "
        "that's all you need. Feel free to hang up",
        "If that's all you need. Please hang up.",
        "Hang up. Or say main menu for other options.",
        "You are welcome to end this call when finished.",
    ],
)
async def test_conditional_menu_hangup_keeps_call_active(service, brief, prompt):
    call_id, _ = await started(service, brief)
    llm = SupervisedRealtimeService(supervisor=service, call_id=call_id, api_key="offline-fixture")
    service.end = AsyncMock()
    await llm.observe(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "menu-farewell",
            "transcript": prompt,
        }
    )
    assert not llm.stop_requested
    assert not llm.request_stop(prompt)
    assert not llm.request_stop("hang up")
    assert not llm.request_stop("Please hang up.")
    assert not llm.request_stop("End the call now.")
    await asyncio.sleep(0)
    service.end.assert_not_awaited()
    assert service.get("owner", call_id)["state"] == "active"


@pytest.mark.parametrize(
    "fragments",
    [
        ["If that is all you need.", "Please hang up."],
        ["You can say repeat that or if that's all you need.", "Feel free to hang up."],
        ["When you're finished,", "End the call."],
    ],
)
async def test_menu_fragments_allow_continuation_and_later_explicit_stop(service, brief, fragments):
    call_id, _ = await started(service, brief)
    llm = SupervisedRealtimeService(supervisor=service, call_id=call_id, api_key="offline-fixture")
    service.end = AsyncMock()

    async def hear(text, identity):
        await llm.observe(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": identity,
                "transcript": text,
            }
        )

    for index, text in enumerate(fragments):
        await hear(text, f"menu-{index}")
        assert not llm.stop_requested
        assert not llm.request_stop("Please hang up now.")
    await llm.observe(
        {
            "type": "response.output_audio_transcript.done",
            "item_id": "continue",
            "transcript": "Main menu.",
        }
    )
    await hear("What else can I help you with?", "next-menu")
    await asyncio.sleep(0)
    service.end.assert_not_awaited()
    assert service.get("owner", call_id)["state"] == "active"

    await hear("Please hang up now.", "real-stop")
    await llm.stop_task
    service.end.assert_awaited_once_with("owner", call_id, "recipient_stop")


async def test_explicit_quoted_hangup_does_not_wait_for_separate_transcriber(service, brief):
    call_id, _ = await started(service, brief)
    llm = SupervisedRealtimeService(supervisor=service, call_id=call_id, api_key="offline-fixture")
    service.end = AsyncMock()
    llm.dialog = [{"role": "user", "text": "Stop the story. Look up closing time."}]
    assert llm.request_stop("Thanks. End the call now.")
    await llm.stop_task
    service.end.assert_awaited_once_with("owner", call_id, "recipient_stop")
    event = service.call("owner", call_id)["usage"]["voice_control_events"][-1]
    assert event["source"] == "model_quote" and event["accepted"]
    assert "Thanks" not in json.dumps(event)


async def test_hangup_tool_rejects_cropped_menu_quotes_and_allows_continuation(service, brief):
    from dots_phone.call_task import task_tools

    call_id, _ = await started(service, brief)
    llm = SupervisedRealtimeService(supervisor=service, call_id=call_id, api_key="offline-fixture")
    service.end = AsyncMock()
    tool = next(
        t for t in task_tools(llm, source_tools=False) if getattr(t, "__name__", "") == "hang_up"
    )
    params = SimpleNamespace(result_callback=AsyncMock())
    await llm.observe(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "menu",
            "transcript": "If that is all you need, please hang up. Otherwise say main menu.",
        }
    )
    await tool(params, recipient_request="Could you please hang up now?")
    feedback = params.result_callback.call_args.args[0]
    assert feedback["ending"] is False and feedback["reason"]
    assert not llm.stop_requested
    await llm.observe(
        {
            "type": "response.output_audio_transcript.done",
            "item_id": "continue",
            "transcript": "Main menu.",
        }
    )
    await llm.observe(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "next",
            "transcript": "Tell me what else you need help with.",
        }
    )
    await asyncio.sleep(0)
    service.end.assert_not_awaited()
    assert service.get("owner", call_id)["state"] == "active"


@pytest.mark.parametrize("failure", ["fatal_error", "exception", "eof"])
async def test_dead_realtime_reader_ends_call_and_retains_only_safe_diagnostics(
    service, brief, failure
):
    call_id, _ = await started(service, brief)
    llm = SupervisedRealtimeService(supervisor=service, call_id=call_id, api_key="offline-fixture")
    llm._handle_evt_error = AsyncMock()

    class BrokenSocket:
        async def __aiter__(self):
            if failure == "fatal_error":
                yield json.dumps(
                    {
                        "type": "error",
                        "event_id": "fixture",
                        "error": {
                            "type": "invalid_request_error",
                            "code": "invalid_value",
                            "message": "private-provider-message",
                        },
                    }
                )
            elif failure == "exception":
                raise ValueError("private-provider-message")

    llm._websocket = BrokenSocket()
    await llm._receive_task_handler()
    await asyncio.wait_for(llm.stop_task, 1)
    result = service.result("owner", call_id)
    assert result["state"] == "failed" and result["outcome"] == "unresolved"
    assert result["termination_reason"] == "voice_path_failed"
    assert result["usage"]["realtime_path_failure"] == {
        "phase": "receive_exception" if failure == "exception" else "receive_ended",
        "exception_type": "ValueError" if failure == "exception" else None,
    }
    if failure == "fatal_error":
        assert result["usage"]["realtime_last_error"]["code"] == "invalid_value"
    assert "private-provider-message" not in json.dumps(service.call("owner", call_id))
    assert len(service.provider.stops) == 1


async def test_input_dispatch_failure_ends_call_instead_of_leaving_silent_carrier(service, brief):
    from pipecat.frames.frames import InputAudioRawFrame

    call_id, _ = await started(service, brief)
    llm = SupervisedRealtimeService(supervisor=service, call_id=call_id, api_key="offline-fixture")
    llm.ready.set()
    llm.input_resampler = SimpleNamespace(resample=AsyncMock(side_effect=RuntimeError("private")))
    llm.input_queue.put_nowait(InputAudioRawFrame(b"\x00\x10" * 160, 8000, 1))
    await llm.dispatch_input()
    await asyncio.wait_for(llm.stop_task, 1)
    result = service.result("owner", call_id)
    assert result["termination_reason"] == "voice_path_failed"
    assert result["usage"]["realtime_path_failure"]["phase"] == "input_dispatch"
    assert result["usage"]["realtime_media_progress"]["input_frames_dispatched"] == 0
    assert "private" not in json.dumps(result["usage"])


async def test_silent_tool_output_is_sent_once_without_creating_response(service, brief):
    call_id, _ = await started(service, brief)
    llm = SupervisedRealtimeService(supervisor=service, call_id=call_id, api_key="offline-fixture")
    llm._websocket = SimpleNamespace(send=AsyncMock())
    llm._create_response = AsyncMock()
    params = SimpleNamespace(tool_call_id="control", result_callback=AsyncMock())
    result = {"carrier_control_accepted": True, "audio_reconnected": True}
    await llm.return_local_tool_result(params, result)
    await llm.return_local_tool_result(params, result)
    wire = json.loads(llm._websocket.send.call_args.args[0])
    assert wire["type"] == "conversation.item.create"
    assert wire["item"]["type"] == "function_call_output"
    assert wire["item"]["call_id"] == "control"
    assert json.loads(wire["item"]["output"]) == result
    assert llm._websocket.send.await_count == params.result_callback.await_count == 1
    assert params.result_callback.call_args.kwargs["properties"].run_llm is False
    llm._create_response.assert_not_awaited()
    assert llm.media_progress["tool_outputs_sent"] == 1


@pytest.mark.parametrize("next_vad_response", [False, True])
async def test_rejected_control_delivers_feedback_during_speech_and_resumes_once(
    service, brief, next_vad_response
):
    call_id, _ = await started(service, brief)
    llm = SupervisedRealtimeService(supervisor=service, call_id=call_id, api_key="offline-fixture")
    llm._websocket = SimpleNamespace(send=AsyncMock())
    llm._create_response = AsyncMock()
    # SDK aggregation can still consider the recipient speaking. The actual
    # observer, not that stale context flag, owns safe continuation timing.
    llm.caller_speaking = True
    params = SimpleNamespace(tool_call_id="refused_key", result_callback=AsyncMock())
    feedback = {"carrier_control_accepted": False, "input_status": "not_submitted"}
    await llm.return_local_tool_result(params, feedback, run_llm=True)
    assert llm._websocket.send.await_count == 1
    assert params.result_callback.call_args.kwargs["properties"].run_llm is False
    llm._create_response.assert_not_awaited()
    await llm.observe(
        {
            "type": "conversation.item.added",
            "item": {"type": "function_call_output", "call_id": "refused_key"},
        }
    )
    if next_vad_response:
        await llm.observe({"type": "response.created", "response": {"id": "natural_turn"}})
    await llm.observe({"type": "input_audio_buffer.speech_stopped"})
    await asyncio.wait_for(llm.source_continuation_task, 1)
    assert llm._create_response.await_count == (0 if next_vad_response else 1)
    assert not llm.source_continuations
    assert llm._websocket.send.await_count == 1


@pytest.mark.parametrize("mode", ["stalled", "quiet_hold", "carrier_gap", "starting"])
async def test_media_watch_detects_stalled_input_without_timing_out_legitimate_silence(
    service, brief, mode
):
    call_id, _ = await started(service, brief)
    llm = SupervisedRealtimeService(supervisor=service, call_id=call_id, api_key="offline-fixture")
    if mode != "starting":
        llm.ready.set()
    for second in range(1, 25):
        if mode == "quiet_hold":
            llm.media_progress["model_audio_packets_sent"] += 5
        if mode == "carrier_gap" and second < 20:
            received_at = 1
        else:
            received_at = second
        llm.check_input_progress(received_at)
    if mode == "stalled":
        await asyncio.wait_for(llm.stop_task, 1)
        assert service.result("owner", call_id)["usage"]["realtime_path_failure"] == {
            "phase": "input_stalled",
            "exception_type": None,
        }
        assert len(service.provider.stops) == 1
    else:
        assert llm.stop_task is None
        assert service.call("owner", call_id)["state"] == "active"


@pytest.mark.parametrize("filter_mode", ["near_field", "far_field"])
async def test_native_candidates_roll_back_on_same_sdk_connection(service, brief, filter_mode):
    from pipecat.services.openai.realtime import events

    from dots_phone.task_voice import general_turn_detection, input_noise_reduction

    def session_settings(detector, noise_filter):
        configured = service.settings.model_copy(
            update={
                "pipecat_general_turn_detection": detector,
                "pipecat_noise_reduction": noise_filter,
            }
        )
        return SupervisedRealtimeService.Settings(
            session_properties=events.SessionProperties(
                audio=events.AudioConfiguration(
                    input=events.AudioInput(
                        noise_reduction=input_noise_reduction(configured),
                        turn_detection=general_turn_detection(configured),
                    )
                )
            )
        )

    cid = service.prepare("owner", brief)["call_id"]
    llm = SupervisedRealtimeService(supervisor=service, call_id=cid, api_key="offline-fixture")
    socket = SimpleNamespace(send=AsyncMock())
    llm._websocket = socket

    # Exercise the SDK's settings replacement and subsequent full update on
    # one connection, including the serializer that otherwise drops nulls.
    await llm._update_settings(session_settings("semantic_vad", filter_mode))
    await llm._update_settings(session_settings("server_vad", "off"))
    await llm._send_session_update()

    payloads = [json.loads(call.args[0]) for call in socket.send.call_args_list]
    assert llm._websocket is socket
    assert [payload["type"] for payload in payloads] == ["session.update"] * 3
    inputs = [payload["session"]["audio"]["input"] for payload in payloads]
    assert inputs[0] == {
        "noise_reduction": {"type": filter_mode},
        "turn_detection": {
            "type": "semantic_vad",
            "eagerness": "high",
            "create_response": True,
            "interrupt_response": True,
        },
    }
    expected_rollback = {
        "noise_reduction": None,
        "turn_detection": {
            "type": "server_vad",
            "threshold": 0.5,
            "prefix_padding_ms": 300,
            "silence_duration_ms": 300,
            "idle_timeout_ms": None,
            "create_response": True,
            "interrupt_response": True,
        },
    }
    assert inputs[1] == expected_rollback
    assert inputs[2] == expected_rollback
