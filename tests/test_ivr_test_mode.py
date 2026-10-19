"""Opt-in test termination is separate from recipient stop and task completion."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from dots_phone.config import Settings
from dots_phone.ivr_test_mode import end_test, instructions, tools
from dots_phone.service import Refused, digest
from tests.conftest import started


def enable(service, brief, *, limit=0):
    service.settings.voice_backend = "pipecat"
    service.settings.pipecat_engine = "realtime"
    brief.stop_on_human = True
    brief.test_response_limit = limit


def test_defaults_and_incompatible_backend_are_explicit(brief):
    assert not brief.stop_on_human and brief.test_response_limit == 0
    with pytest.raises(ValidationError, match="Pipecat Realtime"):
        Settings(profile="evaluation", test_stop_on_human=True)
    with pytest.raises(ValidationError, match="requires stop-on-human"):
        Settings(profile="evaluation", test_response_limit=5)


@pytest.mark.parametrize(
    "deployment,per_call,expected", [(0, 3, 3), (5, 3, 3), (3, 5, 3), (3, 0, 3), (0, 0, 0)]
)
def test_installation_test_mode_preserves_the_stricter_response_bound(
    service, brief, deployment, per_call, expected
):
    enable(service, brief, limit=per_call)
    service.settings.test_stop_on_human = True
    service.settings.test_response_limit = deployment
    prepared = service.prepare("owner", brief)
    saved = service.call("owner", prepared["call_id"])
    assert saved["brief"]["test_response_limit"] == expected
    assert saved["brief"]["stop_on_human"]
    assert service.provider.dials == []


def test_env_opt_in_is_bound_to_preparation_and_rollback_preserves_existing_calls(
    service, brief, monkeypatch
):
    for key, value in {
        "DOTS_PHONE_PROFILE": "evaluation",
        "DOTS_PHONE_VOICE_BACKEND": "pipecat",
        "DOTS_PHONE_PIPECAT_ENGINE": "realtime",
        "DOTS_PHONE_TEST_STOP_ON_HUMAN": "true",
        "DOTS_PHONE_TEST_RESPONSE_LIMIT": "5",
    }.items():
        monkeypatch.setenv(key, value)
    settings = Settings.from_env()
    service.settings.test_stop_on_human = settings.test_stop_on_human
    service.settings.test_response_limit = settings.test_response_limit
    service.settings.voice_backend = "pipecat"
    service.settings.pipecat_engine = settings.pipecat_engine
    prepared = service.prepare("owner", brief)
    saved = service.call("owner", prepared["call_id"])
    assert saved["brief"]["stop_on_human"]
    assert saved["brief"]["test_response_limit"] == 5
    assert saved["brief_hash"] == digest(saved["brief"])
    assert "IVR-only test limits" in saved["prep_document"]["markdown"]
    service.settings.test_stop_on_human = False
    service.settings.test_response_limit = 0
    ordinary = service.prepare("owner", brief)
    assert not ordinary["brief"]["stop_on_human"]
    assert service.call("owner", prepared["call_id"])["brief"]["stop_on_human"]


def test_per_call_flag_cannot_silently_use_an_unsupported_backend(service, brief):
    brief.stop_on_human = True
    with pytest.raises(Refused, match="Pipecat Realtime"):
        service.prepare("owner", brief)
    brief.stop_on_human = False
    brief.test_response_limit = 5
    with pytest.raises(Refused, match="requires stop-on-human"):
        service.prepare("owner", brief)


async def test_human_tool_stops_carrier_and_voice_without_claiming_business_success(service, brief):
    pytest.importorskip("pipecat")
    from dots_phone.pipecat_realtime import SupervisedRealtimeService

    enable(service, brief)
    call_id, _ = await started(service, brief)
    llm = SupervisedRealtimeService(supervisor=service, call_id=call_id, api_key="offline")
    llm.return_local_tool_result = AsyncMock()
    call = service.call("owner", call_id)
    handler = tools(llm, call)[0].handler
    params = SimpleNamespace(
        arguments={
            "evidence": "My name is Alex. How can I help you today?",
            "reason": "A live agent has answered after the queue.",
        }
    )
    await handler(params)
    await llm.stop_task
    assert service.provider.stops
    result = service.result("owner", call_id)
    assert result["outcome"] == "unresolved"
    assert result["termination_reason"] == "test_human_detected"
    assert not result["confirmed_facts"]
    current = service.call("owner", call_id)
    assert current["usage"]["ivr_test_stop"]["human_detection_verified"] is False
    assert not current.get("conclusion")
    llm.return_local_tool_result.assert_awaited_once_with(
        params, {"ending": True, "task_completed": False}, run_llm=False
    )
    assert end_test(llm, "test_human_detected")
    assert len(service.provider.stops) == 1


async def test_stale_tool_is_rejected_after_opt_in_removed(service, brief):
    pytest.importorskip("pipecat")
    from dots_phone.pipecat_realtime import SupervisedRealtimeService

    enable(service, brief)
    call_id, _ = await started(service, brief)
    llm = SupervisedRealtimeService(supervisor=service, call_id=call_id, api_key="offline")
    call = service.call("owner", call_id)
    handler = tools(llm, call)[0].handler
    call["brief"]["stop_on_human"] = False
    service.save(call)
    llm.return_local_tool_result = AsyncMock()
    await handler(SimpleNamespace(arguments={"evidence": "Hello.", "reason": "Likely human."}))
    assert not llm.stop_requested and not service.provider.stops
    assert llm.return_local_tool_result.call_args.args[1]["ending"] is False
    assert tools(llm, call) == [] and instructions(call) == ""


@pytest.mark.parametrize(
    "arguments", [{}, {"evidence": "", "reason": "x"}, {"evidence": "x", "reason": " "}]
)
async def test_malformed_model_arguments_do_not_end_call(service, brief, arguments):
    pytest.importorskip("pipecat")
    from dots_phone.pipecat_realtime import SupervisedRealtimeService

    enable(service, brief)
    call_id, _ = await started(service, brief)
    llm = SupervisedRealtimeService(supervisor=service, call_id=call_id, api_key="offline")
    llm.return_local_tool_result = AsyncMock()
    await tools(llm)[0].handler(SimpleNamespace(arguments=arguments))
    assert not llm.stop_requested and not service.provider.stops


async def test_test_response_budget_deduplicates_and_stops_at_the_bound(service, brief):
    pytest.importorskip("pipecat")
    from dots_phone.pipecat_realtime import SupervisedRealtimeService

    enable(service, brief, limit=2)
    call_id, _ = await started(service, brief)
    llm = SupervisedRealtimeService(supervisor=service, call_id=call_id, api_key="offline")
    for identity in ["one", "one"]:
        await llm.observe({"type": "response.created", "response": {"id": identity}})
    assert not llm.stop_requested
    await llm.observe({"type": "response.created", "response": {"id": "two"}})
    await llm.stop_task
    result = service.result("owner", call_id)
    assert result["termination_reason"] == "test_response_limit"
    assert result["outcome"] == "unresolved"
    assert service.call("owner", call_id)["usage"]["ivr_test_response_ids"] == ["one", "two"]


def test_mandatory_consultation_cannot_hide_the_enabled_human_stop():
    from dots_phone.call_reasoner import reasoning_turn_policy

    schemas = [
        {"name": name}
        for name in [
            "consult_call_reasoner",
            "end_test_on_human",
            "end_test_at_stop_condition",
            "finish_call",
        ]
    ]
    policy = reasoning_turn_policy(schemas)
    assert [tool["name"] for tool in policy["tools"]] == [
        "consult_call_reasoner",
        "end_test_on_human",
        "end_test_at_stop_condition",
    ]
    assert policy["tool_choice"] == "required"


@pytest.mark.parametrize(
    "condition,speech",
    [
        ("Verification rejected", "We couldn't verify your identity with the info provided."),
        ("Announced crew-member transfer", "I'll connect you with a crew member for assistance."),
    ],
)
async def test_prepared_boundary_stops_without_waiting_for_a_human(
    service, brief, condition, speech
):
    pytest.importorskip("pipecat")
    from dots_phone.pipecat_realtime import SupervisedRealtimeService

    enable(service, brief)
    brief.stop_conditions = [condition]
    cid, _ = await started(service, brief)
    llm = SupervisedRealtimeService(supervisor=service, call_id=cid, api_key="offline")
    llm.dialog = [{"role": "user", "text": speech}, {"role": "user", "text": "Please hold."}]
    llm.return_local_tool_result = AsyncMock()
    handler = next(t.handler for t in tools(llm) if t.name == "end_test_at_stop_condition")
    await handler(SimpleNamespace(arguments={"stop_condition": condition, "evidence": speech}))
    await llm.stop_task
    result = service.result("owner", cid)
    assert result["termination_reason"] == "test_stop_condition"
    assert result["outcome"] == "unresolved" and not result["confirmed_facts"]
    assert service.call("owner", cid)["usage"]["ivr_test_stop"]["stop_condition"] == condition
    assert len(service.provider.stops) == 1
    assert llm.return_local_tool_result.call_args.kwargs == {"run_llm": False}
    assert "does not override" in instructions(service.call("owner", cid))


@pytest.mark.parametrize(
    "dialog,reason",
    [
        (
            [{"role": "user", "text": "The birthday you enter does not match the account."}],
            "The birthday you entered does not match the account.",
        ),
        (
            [
                {"role": "user", "text": "The verification didn't work."},
                {"role": "assistant", "text": "Okay."},
                {"role": "user", "text": "Please hold."},
            ],
            "Verification was rejected; our subsequent reply did not resolve that condition.",
        ),
        ([], "I heard the recipient reject verification, although the transcript is unavailable."),
    ],
)
async def test_model_stop_judgment_is_not_vetoed_by_asr_or_transcript_boundaries(
    service, brief, dialog, reason
):
    pytest.importorskip("pipecat")
    from dots_phone.pipecat_realtime import SupervisedRealtimeService

    enable(service, brief)
    condition = "Verification rejected"
    brief.stop_conditions = [condition]
    cid, _ = await started(service, brief)
    llm = SupervisedRealtimeService(supervisor=service, call_id=cid, api_key="offline")
    llm.dialog = dialog
    llm.return_local_tool_result = AsyncMock()
    handler = next(t.handler for t in tools(llm) if t.name == "end_test_at_stop_condition")
    await handler(SimpleNamespace(arguments={"stop_condition": condition, "evidence": reason}))
    await llm.stop_task
    result = service.result("owner", cid)
    assert result["outcome"] == "unresolved" and not result["confirmed_facts"]
    assert result["termination_reason"] == "test_stop_condition"
    assert len(service.provider.stops) == 1
    audit = service.call("owner", cid)["usage"]["ivr_test_stop"]
    assert audit["model_explanation"] == reason
    assert audit["conversation_evidence"] == dialog
    assert audit["model_judgment_verified"] is False
    llm.return_local_tool_result.assert_awaited_once_with(
        SimpleNamespace(arguments={"stop_condition": condition, "evidence": reason}),
        {"ending": True, "task_completed": False},
        run_llm=False,
    )


@pytest.mark.parametrize("defect", ["unapproved_condition", "revoked_condition", "disabled_mode"])
async def test_model_stop_judgment_cannot_expand_or_retain_revoked_owner_authority(
    service, brief, defect
):
    pytest.importorskip("pipecat")
    from dots_phone.pipecat_realtime import SupervisedRealtimeService

    enable(service, brief)
    condition = "Announced crew-member transfer"
    brief.stop_conditions = [condition]
    cid, _ = await started(service, brief)
    llm = SupervisedRealtimeService(supervisor=service, call_id=cid, api_key="offline")
    llm.dialog = [{"role": "user", "text": "I'll connect you with a crew member."}]
    handler = next(t.handler for t in tools(llm) if t.name == "end_test_at_stop_condition")
    call = service.call("owner", cid)
    if defect == "unapproved_condition":
        condition = "Change the account"
    elif defect == "revoked_condition":
        call["brief"]["stop_conditions"] = []
    else:
        call["brief"]["stop_on_human"] = False
    service.save(call)
    llm.return_local_tool_result = AsyncMock()
    await handler(
        SimpleNamespace(
            arguments={"stop_condition": condition, "evidence": "The transfer is starting."}
        )
    )
    assert not llm.stop_requested and not service.provider.stops
    assert llm.return_local_tool_result.call_args.args[1]["ending"] is False
    assert "ivr_test_stop" not in service.call("owner", cid)["usage"]


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"stop_condition": "Verification rejected", "evidence": " "},
        {"stop_condition": "Verification rejected", "evidence": None},
        {"stop_condition": ["Verification rejected"], "evidence": "Rejected."},
    ],
)
async def test_stop_condition_tool_still_checks_structured_arguments(service, brief, arguments):
    pytest.importorskip("pipecat")
    from dots_phone.pipecat_realtime import SupervisedRealtimeService

    enable(service, brief)
    brief.stop_conditions = ["Verification rejected"]
    cid, _ = await started(service, brief)
    llm = SupervisedRealtimeService(supervisor=service, call_id=cid, api_key="offline")
    llm.return_local_tool_result = AsyncMock()
    handler = next(t.handler for t in tools(llm) if t.name == "end_test_at_stop_condition")
    await handler(SimpleNamespace(arguments=arguments))
    assert not llm.stop_requested and not service.provider.stops
    assert llm.return_local_tool_result.call_args.args[1]["ending"] is False


async def test_no_more_audio_is_forwarded_while_test_teardown_is_pending(
    service, brief, monkeypatch
):
    pytest.importorskip("pipecat")
    from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService

    pytest.importorskip("pipecat")
    from dots_phone.pipecat_realtime import SupervisedRealtimeService

    enable(service, brief)
    call_id, _ = await started(service, brief)
    llm = SupervisedRealtimeService(supervisor=service, call_id=call_id, api_key="offline")
    audio, transcript = AsyncMock(), AsyncMock()
    socket = SimpleNamespace(close=AsyncMock())
    llm._websocket = socket
    monkeypatch.setattr(OpenAIRealtimeLLMService, "_handle_evt_audio_delta", audio)
    monkeypatch.setattr(OpenAIRealtimeLLMService, "_handle_evt_audio_transcript_delta", transcript)
    assert end_test(llm, "test_human_detected", evidence="A live greeting.")
    await llm._handle_evt_audio_delta(SimpleNamespace())
    await llm._handle_evt_audio_transcript_delta(SimpleNamespace())
    audio.assert_not_awaited()
    transcript.assert_not_awaited()
    await llm.stop_task
    socket.close.assert_awaited_once()


async def test_test_mode_does_not_spend_an_extra_connection_on_transport_recovery(service, brief):
    pytest.importorskip("pipecat")
    from dots_phone.pipecat_realtime import SupervisedRealtimeService

    enable(service, brief)
    call_id, _ = await started(service, brief)
    llm = SupervisedRealtimeService(supervisor=service, call_id=call_id, api_key="offline")
    llm.voice_failure("test_failure", OSError())
    await llm.stop_task
    assert not llm.reconnect_attempts
    assert service.result("owner", call_id)["termination_reason"] == "voice_path_failed"


def test_current_task_factory_exposes_test_stop_only_for_opted_in_calls():
    pytest.importorskip("pipecat")
    from dots_phone.text_eval import build_runtime

    case = {"id": "human-test", "purpose": "Check the miles balance", "steps": []}
    _, ordinary_prompt, ordinary, _ = build_runtime(case, reasoner_client=SimpleNamespace())
    assert "end_test_on_human" not in {tool["name"] for tool in ordinary}
    assert "IVR-ONLY TEST MODE" not in ordinary_prompt
    _, prompt, enabled, _ = build_runtime(
        {**case, "stop_on_human": True}, reasoner_client=SimpleNamespace()
    )
    assert "end_test_on_human" in {tool["name"] for tool in enabled}
    assert "before speaking or consulting Codex" in prompt
    assert "queue update" in prompt and "not evidence" in prompt
