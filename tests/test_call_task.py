import json
from decimal import Decimal

import pytest

from dots_phone.call_task import (
    RESULT_ACTION,
    SIMULATED_CONSTRAINT,
    SOURCE_ACTION,
    recipient_evidence,
    task_conclusion,
)
from dots_phone.local_gmail_worker import private_json
from dots_phone.local_trip_mail_worker import LocalTripMailWorker
from dots_phone.models import Brief
from dots_phone.service import Refused


async def test_silent_wait_preserves_pending_work_and_emits_no_response(service, brief, tmp_path):
    pytest.importorskip("pipecat")
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from dots_phone.call_task import task_tools
    from dots_phone.task_voice import create_task_voice

    call, _ = configured_task(service, brief, tmp_path)
    service.settings.pipecat_engine = "realtime"
    llm, context, _ = create_task_voice(service, call, api_key="offline-fixture")
    pending = service.pending_decision(call["call_id"], "lookup", "A pending question")
    llm._create_response = AsyncMock()
    tool = next(t for t in task_tools(llm) if getattr(t, "__name__", "") == "wait_for_recipient")
    llm._send_tool_result = AsyncMock()
    params = SimpleNamespace(tool_call_id="quiet_wait", result_callback=AsyncMock())
    await tool(params)
    llm._send_tool_result.assert_awaited_once_with("quiet_wait", '{"listening": true}')
    assert params.result_callback.call_args.args == ({"listening": True},)
    assert params.result_callback.call_args.kwargs["properties"].run_llm is False
    llm._create_response.assert_not_awaited()
    current = service.call("owner", call["call_id"])
    assert current["state"] == "awaiting_decision"
    assert current["pending"]["request_id"] == pending["request_id"]
    assert "wait_for_recipient" in {t.name for t in context.tools.standard_tools}


def test_confirmed_task_facts_are_heard_words_not_unverified_paraphrases(service, brief, tmp_path):
    call, _ = configured_task(service, brief, tmp_path)
    words = "No account changes have been made."
    report = {
        "outcome": "unresolved",
        "evidence": [
            {
                "criterion_id": "conditions",
                "text": "No additional verification was required; no account changes were made.",
                "quote": words,
            }
        ],
        "unresolved_questions": ["The requested balance remains unknown."],
    }
    result = task_conclusion(call, report, [{"role": "user", "text": words}])
    assert "verification" not in result.confirmed_facts[0].text
    assert result.confirmed_facts[0].text == result.confirmed_facts[0].quote


def test_old_task_result_returns_quoted_facts_without_rewriting_original_report(
    service, brief, tmp_path
):
    call, _ = configured_task(service, brief, tmp_path)
    claim = {
        "text": "No additional verification was required; no account changes were made.",
        "quote": "No account changes have been made.",
        "action_name": None,
    }
    call["conclusion"] = {
        "outcome": "unresolved",
        "confirmed_facts": [claim],
        "commitments": [],
        "unresolved_questions": ["The balance is unknown."],
    }
    service.terminal(call, "completed", "task_finished")
    returned = service.result("owner", call["call_id"])
    assert returned["confirmed_facts"][0]["text"] == claim["quote"]
    stored = service.call("owner", call["call_id"])
    assert stored["result"]["confirmed_facts"][0]["text"] == claim["text"]
    assert returned["outcome"] == stored["result"]["outcome"] == "unresolved"
    summary = json.loads(service.terminal_summary(stored))
    assert summary["confirmed"] == claim["quote"]


def configured_task(service, brief, tmp_path):
    data = brief.model_dump(mode="json")
    data.update(
        actions=[SOURCE_ACTION, RESULT_ACTION, "request_change"],
        constraints=[SIMULATED_CONSTRAINT],
        completion_criteria=[
            {"id": "goal", "description": "Recipient establishes the requested outcome."},
            {"id": "conditions", "description": "Recipient establishes applicable requirements."},
        ],
        max_seconds=300,
        budget_usd=str(Decimal("3")),
    )
    cid = service.prepare("owner", Brief.model_validate(data))["call_id"]
    call = service.call("owner", cid)
    call.update(state="active", provider_sid="CA" + "1" * 32)
    service.save(call)
    directory = tmp_path / "sources"
    directory.mkdir(mode=0o700)
    private_json(
        directory / "registry.json",
        {
            "schema": 6,
            "cases": [
                {
                    "call_id": cid,
                    "revision": 1,
                    "brief_hash": call["brief_hash"],
                    "provider": "call_sources",
                    "max_requests": 10,
                    "mail_scope": {},
                    "fixture_sources": [
                        {
                            "source_id": "record",
                            "subject": "Task record",
                            "text": "Relevant source text.",
                        }
                    ],
                }
            ],
        },
    )
    worker = LocalTripMailWorker(service, directory)
    service.local_context_worker = worker
    return call, worker


@pytest.mark.parametrize(
    "quote",
    [
        "Jordan is authorized to arrive alone with photo ID.",
        "You're now booked for October 22 at eleven. Bring vaccination records.",
        "Your device qualifies for free repair; return shipping is nine dollars.",
    ],
)
async def test_natural_outcome_reaches_authoritative_call_result(service, brief, tmp_path, quote):
    call, worker = configured_task(service, brief, tmp_path)
    service.observe_transcript(call["call_id"], [{"role": "user", "text": quote}])
    report = {
        "outcome": "completed",
        "evidence": [
            {
                "criterion_id": key,
                "text": text,
                "quote": "A paraphrase rather than the observed words.",
            }
            for key, text in [
                ("goal", "Requested outcome confirmed."),
                ("conditions", "Requirements confirmed."),
            ]
        ],
        "unresolved_questions": [],
    }
    pending = service.pending_decision(
        call["call_id"],
        "result-tool",
        json.dumps({"operation": "complete", "arguments": {"report": report}}),
    )
    await worker.answer(call, pending)
    feedback = json.loads(service.provider.updates[-1][1])
    assert feedback["result_recorded"] is False
    assert len(json.dumps(feedback, ensure_ascii=False)) <= 1500
    reference = next(item["id"] for item in feedback["recipient_evidence"] if item["text"] == quote)
    for claim in report["evidence"]:
        claim.pop("quote")
        claim["evidence_id"] = reference
    pending = service.pending_decision(
        call["call_id"],
        "corrected-result-tool",
        json.dumps({"operation": "complete", "arguments": {"report": report}}),
    )
    await worker.answer(call, pending)
    current = service.call("owner", call["call_id"])
    assert current["conclusion"]["outcome"] == "completed"
    service.terminal(current, "completed", "recipient_stop")
    result = service.result("owner", call["call_id"])
    assert result["outcome"] == "completed"
    assert all(e["quote"] in quote for e in result["confirmed_facts"])
    assert result["revision"] == call["revision"] and result["brief_hash"] == call["brief_hash"]
    assert "can check in without" not in quote


def test_refusal_does_not_require_success_criteria(service, brief, tmp_path):
    call, _ = configured_task(service, brief, tmp_path)
    quote = "That slot is unavailable; only the following day is open."
    report = {
        "outcome": "unresolved",
        "evidence": [
            {
                "criterion_id": "goal",
                "text": "Requested slot unavailable; original booking retained.",
                "quote": quote,
            }
        ],
        "unresolved_questions": ["Owner has not authorized the alternative slot."],
    }
    result = task_conclusion(call, report, [{"role": "user", "text": quote}])
    assert result.outcome == "unresolved" and result.unresolved_questions


def test_separate_observed_facts_can_support_the_same_approved_criterion(service, brief, tmp_path):
    call, _ = configured_task(service, brief, tmp_path)
    statements = [
        ("goal", "The available credit is twelve dollars."),
        ("goal", "Two dollars are pending and excluded."),
        ("conditions", "Please bring photo ID."),
    ]
    report = {
        "outcome": "completed",
        "evidence": [
            {"criterion_id": key, "text": text, "quote": text} for key, text in statements
        ],
        "unresolved_questions": [],
    }
    conclusion = task_conclusion(
        call, report, [{"role": "user", "text": text} for _, text in statements]
    )
    assert conclusion.outcome == "completed"
    assert len(conclusion.confirmed_facts) == 3
    assert all(f.text.rstrip(".") == f.quote for f in conclusion.confirmed_facts)
    report["evidence"].pop()
    with pytest.raises(Refused, match="lacks required evidence"):
        task_conclusion(call, report, [{"role": "user", "text": text} for _, text in statements])


def test_four_required_criteria_can_use_separately_quoted_facts(service, brief, tmp_path):
    call, _ = configured_task(service, brief, tmp_path)
    call["brief"]["completion_criteria"] = [
        {"id": key, "description": "Establish the approved requirement.", "required": True}
        for key in ["goal", "conditions", "verification", "delivery"]
    ]
    statements = [
        ("goal", "Twelve dollars are available."),
        ("goal", "Two dollars remain pending."),
        ("conditions", "There is no fee."),
        ("verification", "The supplied details match."),
        ("delivery", "The confirmation arrives tomorrow."),
    ]
    report = {
        "outcome": "completed",
        "evidence": [
            {"criterion_id": key, "text": text, "quote": text} for key, text in statements
        ],
        "unresolved_questions": [],
    }
    conclusion = task_conclusion(
        call, report, [{"role": "user", "text": text} for _, text in statements]
    )
    assert len(conclusion.confirmed_facts) == 5
    assert conclusion.confirmed_facts[1].quote == "Two dollars remain pending"


def test_evidence_chunks_preserve_complete_supported_sentences():
    statements = [
        "The name, address and contact details you supplied match our current membership record.",
        "The amount currently available to spend is forty-eight dollars and seventy cents.",
        "Another six dollars remain pending and are excluded from the amount available.",
    ]
    entries = [{"role": "user", "text": " ".join(statements)}]
    catalog = recipient_evidence(entries)
    for statement in statements:
        assert any(statement in item["text"] for item in catalog)
    assert all(len(item["text"]) <= 240 for item in catalog)


@pytest.mark.parametrize(
    "bad", ["fabricated_quote", "unknown_criterion", "missing_required", "unresolved_success"]
)
def test_result_rejects_unobserved_or_incomplete_success(service, brief, tmp_path, bad):
    call, _ = configured_task(service, brief, tmp_path)
    quote = "The requested change is done; photo ID is required."
    report = {
        "outcome": "completed",
        "evidence": [
            {"criterion_id": key, "text": "Confirmed.", "quote": quote}
            for key in ["goal", "conditions"]
        ],
        "unresolved_questions": [],
    }
    if bad == "fabricated_quote":
        report["evidence"][0]["quote"] = "The fee has been waived."
    elif bad == "unknown_criterion":
        report["evidence"][0]["criterion_id"] = "new_permission"
    elif bad == "missing_required":
        report["evidence"].pop()
    else:
        report["unresolved_questions"] = ["The requested change is still uncertain."]
    with pytest.raises(Refused):
        task_conclusion(call, report, [{"role": "user", "text": quote}])


def test_shared_source_scope_still_binds_owner_revision_and_hash(service, brief, tmp_path):
    call, worker = configured_task(service, brief, tmp_path)
    assert worker.scope(call)
    for key, value in [("owner", "someone_else"), ("revision", 2), ("brief_hash", "f" * 64)]:
        with pytest.raises(Refused):
            worker.scope({**call, key: value})


@pytest.mark.parametrize("farewell", ["Thank you, goodbye.", "Have a great day."])
async def test_natural_farewell_requires_an_already_recorded_outcome(
    service, brief, tmp_path, farewell
):
    pytest.importorskip("pipecat")
    from dots_phone.pipecat_realtime import SupervisedRealtimeService

    call, _ = configured_task(service, brief, tmp_path)
    llm = SupervisedRealtimeService(
        supervisor=service, call_id=call["call_id"], api_key="offline-fixture"
    )
    llm.dialog = [{"role": "user", "text": farewell}]
    assert not llm.request_stop(farewell)
    call["conclusion"] = {
        "outcome": "unresolved",
        "confirmed_facts": [],
        "commitments": [],
        "unresolved_questions": ["Requested slot unavailable."],
    }
    service.save(call)
    assert llm.request_stop(farewell)
    await llm.stop_task


async def test_controller_closes_observed_farewell_without_another_model_tool(
    service, brief, tmp_path
):
    pytest.importorskip("pipecat")
    from dots_phone.pipecat_realtime import SupervisedRealtimeService

    call, _ = configured_task(service, brief, tmp_path)
    llm = SupervisedRealtimeService(
        supervisor=service, call_id=call["call_id"], api_key="offline-fixture"
    )
    event = {
        "type": "conversation.item.input_audio_transcription.completed",
        "transcript": "Have a great day.",
    }
    await llm.observe({**event, "item_id": "before-result"})
    assert llm.stop_task is None
    call["conclusion"] = {
        "outcome": "declined",
        "confirmed_facts": [],
        "commitments": [],
        "unresolved_questions": [],
    }
    service.save(call)
    await llm.observe({**event, "item_id": "after-result"})
    await llm.stop_task
    assert service.result("owner", call["call_id"])["termination_reason"] == "recipient_stop"


def test_natural_quote_mapping_preserves_casefold_expansions():
    from dots_phone.call_task import observed_quote

    assert (
        observed_quote([{"role": "user", "text": "Die Straße ist frei."}], "STRASSE IST FREI")
        == "Straße ist frei"
    )


async def test_shared_read_tool_preserves_voice_selected_focus_and_scope(service, brief, tmp_path):
    pytest.importorskip("pipecat")
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from dots_phone.call_task import task_tools

    _, worker = configured_task(service, brief, tmp_path)
    llm = SimpleNamespace(request_supervision=AsyncMock())
    params = SimpleNamespace()
    await task_tools(llm)[1](params, "f-" + "a" * 32, focus="service duration")
    command = llm.request_supervision.call_args.args[1]
    assert llm.request_supervision.call_args.kwargs == {"defer_source_speech": True}
    operation, arguments = worker.operation({"question": command})
    assert operation == "read"
    assert arguments["focus"] == "service duration"
    assert arguments["offset"] == 0
    command = json.loads(command)
    command["arguments"]["focus"] = "x" * 81
    with pytest.raises(Refused):
        worker.operation({"question": json.dumps(command)})


def test_factory_uses_identical_deployed_tools_and_settings_for_task_data(service, brief, tmp_path):
    pytest.importorskip("pipecat")
    from dots_phone.task_voice import create_task_voice

    call, _ = configured_task(service, brief, tmp_path)
    service.settings.pipecat_engine = "realtime"
    llm, context, prompt = create_task_voice(service, call)
    assert llm._context is context
    assert set(llm._functions) == {
        "search_call_sources",
        "read_call_source",
        "finish_call",
        "hang_up",
        "wait_for_recipient",
    }
    assert llm._settings.session_properties.max_output_tokens == 1024
    assert llm._settings.session_properties.reasoning.effort == "medium"
    detector = llm._settings.session_properties.audio.input.turn_detection
    assert detector.type == "semantic_vad" and detector.eagerness == "high"
    assert "can check in without" not in prompt
    assert "to the client supervisor" not in prompt
    from pipecat.adapters.services.open_ai_realtime_adapter import OpenAIRealtimeLLMAdapter

    tools = OpenAIRealtimeLLMAdapter().to_provider_tools_format(context.tools)
    result = next(tool for tool in tools if tool["name"] == "finish_call")
    parameters = result["parameters"]
    assert set(parameters["required"]) == {"outcome", "evidence", "unresolved_questions"}
    assert parameters["properties"]["outcome"]["enum"] == ["completed", "declined", "unresolved"]
    assert (
        parameters["properties"]["evidence"]["items"]["properties"]["quote"]["anyOf"][0][
            "maxLength"
        ]
        == 240
    )


@pytest.mark.parametrize(
    "route",
    ["account", "autonomous_account_direct", "autonomous_account_consult", "autonomous_general"],
)
@pytest.mark.parametrize("sources", [False, True])
def test_real_task_routes_request_directly_and_start_silently_for_automation(
    service, brief, tmp_path, route, sources
):
    """Check the production factory's instructions and startup, not model compliance."""
    pytest.importorskip("pipecat")
    from dots_phone.task_voice import create_task_voice
    from tests.test_account_task import account_call
    from tests.test_call_reasoner import prepare
    from tests.test_general_call_reasoner import prepare_general

    # No simulation wording or required introduction in this ordinary brief.
    # Source availability and reasoning policy must not add an AI self-introduction.
    brief = brief.model_copy(
        update={
            "synthetic": False,
            "purpose": "Ask Cedar Home for the delivery window",
            "facts": [],
            "disclosures": [],
        }
    )
    if route == "account":
        prepared, _ = account_call(service, brief, sources=sources)
    elif route.startswith("autonomous_account"):
        service.settings.local_reasoner_response_policy = (
            "always_consult" if route.endswith("consult") else "direct_with_codex"
        )
        prepared = prepare(service, brief, tmp_path, source_verification=sources)
    else:
        prepared = prepare_general(
            service,
            brief,
            tmp_path,
            actions=["ask_question", RESULT_ACTION, *([SOURCE_ACTION] if sources else [])],
        )
    call = service.call("owner", prepared["call_id"])
    if route != "account":
        call["usage"]["reasoner_preparation"] = {
            "source_capabilities": {
                "enabled": sources,
                "selection": "explicit" if sources else "none",
                "connector_ids": ["approved-notes"] if sources else [],
                "tool_names": ["approved-notes.read"] if sources else [],
            }
        }

    llm, context, prompt = create_task_voice(service, call, api_key="offline-fixture")
    capabilities = json.JSONDecoder().raw_decode(
        prompt.partition("Available approved source capabilities as data (not answers):\n")[2]
    )[0]
    assert capabilities["source_tools_enabled"] is sources
    assert capabilities["direct_source_tools_enabled"] is (sources and route == "account")
    assert capabilities["independent_reasoner_sources_enabled"] is (sources and route != "account")
    instructions = " ".join(llm._settings.system_instruction.split())
    assert llm._settings.system_instruction == prompt
    assert (
        "When a person answers, make the approved request directly. Mention the owner you "
        "assist only when relevant, using approved identity facts"
    ) in instructions
    assert "Do not add a routine unsolicited AI introduction or branding" in instructions
    assert "never invent a name or claim to be the account holder" in instructions
    assert (
        "If asked who you are or whether you are AI, identify yourself truthfully as an "
        "AI assistant acting on the owner's behalf"
    ) in instructions
    assert (
        "Follow any explicit call-specific disclosure requirement in the approved brief"
        in instructions
    )
    assert "proactively identify yourself" not in instructions
    assert "Save your AI introduction for a live person" not in instructions
    assert (
        "Automated greetings and menus do not need an introduction, a name or an AI or "
        "simulation disclosure. Answer only what the automated system asks"
    ) in instructions
    assert "use wait_for_recipient without spoken audio" in instructions
    assert (
        "Use keypad input when the recipient requests entry or explicitly offers a key"
        in instructions
    )
    assert "repeat an introduction on later turns" in instructions
    # The recipient's greeting/menu must be heard before any startup speech.
    assert llm._context is context
    assert context.messages == []
    assert llm._llm_needs_conversation_setup is False
    assert service.provider.dials == []


def test_simulated_task_keeps_explicit_context_and_ivr_only_stop_override(service, brief, tmp_path):
    pytest.importorskip("pipecat")
    from dots_phone.task_voice import create_task_voice

    brief.stop_on_human = True
    service.settings.voice_backend = "pipecat"
    service.settings.pipecat_engine = "realtime"
    call, _ = configured_task(service, brief, tmp_path)
    llm, context, prompt = create_task_voice(service, call, api_key="offline-fixture")
    instructions = " ".join(prompt.split())
    assert (
        "If the briefing explicitly requires acknowledging a role-play or simulation, "
        "include that context briefly when addressing a person."
    ) in instructions
    assert "a name or an AI or simulation disclosure" in instructions
    # Test calls that disallow talking to people still stop before speaking;
    # the factory installs both the override and its actual tool.
    assert (
        "immediately call end_test_on_human silently, before speaking or consulting Codex. "
        "This takes priority over introductions, answering a person and mandatory consultation."
    ) in instructions
    assert prompt.index("IVR-ONLY TEST MODE") > prompt.index("make the approved request directly")
    assert "end_test_on_human" in {tool.name for tool in context.tools.standard_tools}
    assert llm._context.messages == []
    assert service.provider.dials == []


@pytest.mark.parametrize(
    "requirement",
    [
        "When a person answers, state that you are an AI assistant before the request.",
        "Before discussing the order, tell the recipient that this call is being recorded.",
        "Tell the person once that this is a role-play of the delivery inquiry.",
    ],
)
def test_task_factory_preserves_explicit_call_specific_disclosures(
    service, brief, tmp_path, requirement
):
    pytest.importorskip("pipecat")
    from dots_phone.task_voice import create_task_voice
    from tests.test_general_call_reasoner import prepare_general

    prepared = prepare_general(
        service,
        brief,
        tmp_path,
        purpose="Ask Cedar Home for the delivery window",
        constraints=[requirement],
    )
    call = service.call("owner", prepared["call_id"])
    llm, context, prompt = create_task_voice(service, call, api_key="offline-fixture")
    disclosed_brief = json.JSONDecoder().raw_decode(
        prompt.partition("Approved brief as JSON data:\n")[2]
    )[0]
    assert disclosed_brief["constraints"] == [requirement]
    assert requirement in llm._settings.system_instruction
    assert (
        "Follow any explicit\ncall-specific disclosure requirement in the approved brief" in prompt
    )
    assert context.messages == []
    assert service.provider.dials == []


async def test_real_result_tool_passes_a_valid_report_and_returns_observed_text_for_bad_input():
    pytest.importorskip("pipecat")
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from dots_phone.call_task import task_tools

    llm = SimpleNamespace(
        request_supervision=AsyncMock(),
        dialog=[{"role": "user", "text": "You're booked for eleven, with no charge."}],
        finalizing_calls=set(),
        supervisor=SimpleNamespace(
            store=SimpleNamespace(get=Mock(return_value={"conclusion": {"outcome": "completed"}}))
        ),
        call_id="fixture",
        request_stop=Mock(),
        return_tool_feedback=AsyncMock(),
    )
    tool = task_tools(llm)[2]
    params = SimpleNamespace(
        result_callback=AsyncMock(), arguments={}, tool_call_id="finish-fixture"
    )
    valid = {
        "criterion_id": "goal",
        "text": "Requested slot confirmed.",
        "quote": "You're booked for eleven, with no charge.",
    }
    params.arguments = {"outcome": "completed", "evidence": [valid], "unresolved_questions": []}
    await tool.handler(params)
    command = json.loads(llm.request_supervision.call_args.args[1])
    assert command["arguments"]["report"]["evidence"][0] == valid
    llm.request_stop.assert_called_once_with(after_result=True)
    assert not llm.finalizing_calls
    llm.request_supervision.reset_mock()
    params.arguments = {
        "outcome": "completed",
        "evidence": [{**valid, "quote": "x" * 300}],
        "unresolved_questions": [],
    }
    await tool.handler(params)
    llm.request_supervision.assert_not_awaited()
    feedback = llm.return_tool_feedback.call_args.args[1]
    assert feedback["result_recorded"] is False
    assert any("no charge" in item["text"] for item in feedback["recipient_evidence"])


async def test_finishing_task_requires_a_durable_result_but_no_prescribed_farewell(
    service, brief, tmp_path
):
    pytest.importorskip("pipecat")
    from dots_phone.pipecat_realtime import SupervisedRealtimeService

    call, _ = configured_task(service, brief, tmp_path)
    llm = SupervisedRealtimeService(
        supervisor=service, call_id=call["call_id"], api_key="offline-fixture"
    )
    llm.dialog = [{"role": "user", "text": "You're all set; have a lovely afternoon."}]
    assert not llm.request_stop(after_result=True)
    call["conclusion"] = {
        "outcome": "completed",
        "confirmed_facts": [],
        "commitments": [],
        "unresolved_questions": [],
    }
    service.save(call)
    assert llm.request_stop(after_result=True)
    await llm.stop_task
    assert service.result("owner", call["call_id"])["termination_reason"] == "task_finished"
    assert service.result("owner", call["call_id"])["state"] == "completed"


def test_evidence_reference_resolves_only_observed_recipient_words(service, brief, tmp_path):
    call, _ = configured_task(service, brief, tmp_path)
    entries = [
        {"role": "assistant", "text": "An unverified invented promise."},
        {
            "role": "user",
            "text": "I cannot authorize that. The booking holder must contact us personally.",
        },
    ]
    catalog = recipient_evidence(entries)
    assert len(catalog) == 1 and catalog[0]["text"] == entries[1]["text"]
    report = {
        "outcome": "unresolved",
        "evidence": [
            {
                "criterion_id": "goal",
                "text": "Holder must contact the business personally.",
                "evidence_id": catalog[0]["id"],
            }
        ],
        "unresolved_questions": ["Requested authorization was refused."],
    }
    result = task_conclusion(call, report, entries)
    assert result.confirmed_facts[0].quote == entries[1]["text"]
    report["evidence"][0]["evidence_id"] = "0" * 16
    with pytest.raises(Refused, match="observed"):
        task_conclusion(call, report, entries)


async def test_rejected_reports_keep_tools_and_allow_recovery_without_forced_hangup(
    service, brief, tmp_path
):
    pytest.importorskip("pipecat")
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from dots_phone.task_voice import create_task_voice

    call, _ = configured_task(service, brief, tmp_path)
    service.settings.pipecat_engine = "realtime"
    llm, _, _ = create_task_voice(service, call, api_key="offline-fixture")
    llm._websocket = SimpleNamespace(send=AsyncMock())
    llm.push_frame = AsyncMock()
    llm.start_processing_metrics = AsyncMock()
    llm.start_ttfb_metrics = AsyncMock()
    for _ in range(8):
        await llm.retry_final_report()
    response = json.loads(llm._websocket.send.call_args_list[0].args[0])["response"]
    assert response["tool_choice"] == "auto"
    assert {tool["name"] for tool in response["tools"]} >= {
        "finish_call",
        "search_call_sources",
        "read_call_source",
        "hang_up",
    }
    assert llm._websocket.send.await_count == 8
    assert llm.report_rejections == 8
    assert not llm.stop_requested and llm.stop_task is None
    assert service.call("owner", call["call_id"])["state"] == "active"
    assert not service.call("owner", call["call_id"]).get("conclusion")


async def test_source_budget_exhaustion_does_not_prevent_final_reporting(service, brief, tmp_path):
    call, worker = configured_task(service, brief, tmp_path)
    call["usage"]["local_context_worker"] = {"requests": [{"operation": "search"}] * 10}
    service.save(call)
    quote = "The requested change is done; photo ID is required."
    service.observe_transcript(call["call_id"], [{"role": "user", "text": quote}])
    report = {
        "outcome": "completed",
        "evidence": [
            {"criterion_id": key, "text": "Confirmed by recipient.", "quote": quote}
            for key in ["goal", "conditions"]
        ],
        "unresolved_questions": [],
    }
    pending = service.pending_decision(
        call["call_id"],
        "result-tool",
        json.dumps({"operation": "complete", "arguments": {"report": report}}),
    )
    await worker.answer(call, pending)
    current = service.call("owner", call["call_id"])
    assert current["conclusion"]["outcome"] == "completed"
    pending = service.pending_decision(
        call["call_id"],
        "over-budget-source",
        json.dumps({"operation": "search", "arguments": {"query": "another source"}}),
    )
    with pytest.raises(Refused, match="limit reached"):
        await worker.answer(current, pending)
