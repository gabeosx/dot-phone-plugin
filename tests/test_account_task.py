import asyncio
import json
from datetime import timedelta

import pytest

from dots_phone.account_task import (
    ACCOUNT_ACTION,
    ACCOUNT_CONSTRAINT,
    MENU_ACTION,
    RESULT_ACTION,
    SOURCE_ACTION,
    VERIFY_ACTION,
    AccountTaskWorker,
    account_menu_option,
    verification_digits,
)
from dots_phone.call_task import recipient_evidence
from dots_phone.models import Brief, Decision, PreparedSource, Start, Steer, now
from dots_phone.service import Refused, digest, handles_locally
from tests.conftest import subscribed


def account_call(service, brief, *, synthetic=False, sources=False):
    service.settings.voice_backend = "pipecat"
    service.settings.pipecat_engine = "realtime"
    service.settings.account_tasks = True
    worker = AccountTaskWorker(service)
    service.local_context_worker = worker
    data = brief.model_dump(mode="json")
    data.update(
        synthetic=synthetic,
        actions=[
            ACCOUNT_ACTION,
            RESULT_ACTION,
            MENU_ACTION,
            VERIFY_ACTION,
            *([SOURCE_ACTION] if sources else []),
        ],
        constraints=[ACCOUNT_CONSTRAINT],
        completion_criteria=[{"id": "balance", "description": "Current available balance heard."}],
    )
    prepared = service.prepare("owner", Brief.model_validate(data))
    return prepared, worker


async def test_real_account_preparation_never_authorizes_or_uses_synthetic_campaign(service, brief):
    prepared, worker = account_call(service, brief)
    cid = prepared["call_id"]
    assert prepared["approval_requirements"][0] == "authenticated_owner_approval"
    assert prepared["authorization_id"] is None and prepared["campaign_id"] is None
    assert prepared["calling_worker_mode"] == "manual"
    assert worker.scope(service.call("owner", cid))["source_tools"] is False
    await subscribed(service, cid)
    service.settings.dry_run = False
    with pytest.raises(Refused, match="authorization"):
        service.start("owner", Start(call_id=cid, revision=1, authorization_id="invented"))
    assert not service.provider.dials


@pytest.mark.parametrize("sources", [False, True])
def test_preparation_reports_the_actual_host_information_capability(service, brief, sources):
    prepared, worker = account_call(service, brief, sources=sources)
    call = service.call("owner", prepared["call_id"])
    enabled = prepared["information_capabilities"]["can_request_host_information"]
    assert enabled is sources
    assert enabled == worker.scope(call)["host_sources"]
    assert (SOURCE_ACTION in call["brief"]["actions"]) is sources
    assert any("missing information" in w for w in prepared["preparation_warnings"]) is not sources
    assert prepared["authorization_id"] is None
    assert not service.provider.dials


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("verification", [False, True])
def test_preparation_reports_scoped_keypad_availability(service, brief, enabled, verification):
    pytest.importorskip("pipecat")
    from unittest.mock import MagicMock

    from dots_phone.pipecat_voice import PipecatTelephony

    service.settings.pipecat_account_dtmf = enabled
    service.provider = PipecatTelephony(service.settings, service, MagicMock())
    prepared, worker = account_call(service, brief, sources=True)
    if not verification:
        data = prepared["brief"]
        data["actions"].remove(VERIFY_ACTION)
        prepared = service.prepare("owner", Brief.model_validate(data))
    call = service.call("owner", prepared["call_id"])
    actual = service.provider.account_keypad_enabled(call)
    assert actual is enabled
    assert prepared["interaction_capabilities"] == {
        "keypad_menu": actual,
        "keypad_verification": actual and verification,
        "owner_voice_join": False,
    }
    assert any("Keypad" in w for w in prepared["preparation_warnings"]) is not enabled
    assert worker.scope(call)["read_only_account"]
    assert call["provider_sid"] is None


def test_account_task_uses_verified_task_destination_without_number_setup(service, brief):
    service.settings.destination_policy = "task_scope"
    service.settings.allowed_numbers = []
    brief = brief.model_copy(update={"destination": "+12125550201"})
    prepared, worker = account_call(service, brief, sources=True)
    assert worker.scope(service.call("owner", prepared["call_id"]))["read_only_account"]
    assert not service.provider.dials


@pytest.mark.parametrize("steered", [False, True])
async def test_account_report_uses_actual_recipient_evidence_without_mail(service, brief, steered):
    prepared, worker = account_call(service, brief)
    cid = prepared["call_id"]
    await subscribed(service, cid)
    authorization = service.approve("owner", cid)
    service.settings.dry_run = False
    service.start("owner", Start(call_id=cid, revision=1, authorization_id=authorization))
    await service.tick()
    if steered:
        await service.steer(
            "owner",
            Steer(call_id=cid, revision=1, instruction="Continue toward the requested balance."),
        )
    call = service.call("owner", cid)
    quote = "Your current redeemable balance is 37,412 points; pending points are excluded."
    service.observe_transcript(cid, [{"role": "user", "text": quote}])
    pending = service.pending_decision(
        cid,
        "report",
        json.dumps(
            {
                "operation": "complete",
                "arguments": {
                    "report": {
                        "outcome": "completed",
                        "evidence": [{"criterion_id": "balance", "text": quote, "quote": quote}],
                        "unresolved_questions": [],
                    }
                },
            }
        ),
    )
    await worker.answer(call, pending)
    call = service.call("owner", cid)
    assert call["conclusion"]["confirmed_facts"][0]["quote"] == quote.rstrip(".")
    service.terminal(call, "completed", "task_finished")
    assert service.result("owner", cid)["outcome"] == "completed"


@pytest.mark.parametrize("change", ["mutation", "hash", "owner", "destination", "disabled"])
def test_account_capability_rejects_scope_changes(service, brief, change):
    prepared, worker = account_call(service, brief)
    call = service.call("owner", prepared["call_id"])
    if change == "mutation":
        call["brief"]["actions"].append("redeem_miles")
    elif change == "hash":
        call["brief_hash"] = "0" * 64
    elif change == "owner":
        call["owner"] = "other"
    elif change == "destination":
        service.settings.allowed_numbers = []
    else:
        service.settings.account_tasks = False
    with pytest.raises(Refused):
        worker.scope(call)


@pytest.mark.parametrize(
    "digit,quote,heard,allowed",
    [
        (
            "2",
            "For mileage balance press two",
            "For flights press one. For mileage balance press two.",
            True,
        ),
        ("0", "Press zero for an agent", "Press zero for an agent", True),
        ("2", "Account balance is option two", "To hear your miles, use two", True),
        ("3", "To hear today’s total use three", "To hear today’s total use three", True),
        (
            "1",
            "Press one for flights. Press two for account information",
            "Press one for flights. Press two for account information",
            True,
        ),
        ("12", "For account information press one", "For account information press one", False),
        (
            "2",
            "Use two for assistance",
            "Use three for assistance",
            True,
        ),
    ],
)
def test_account_menu_bounds_control_while_model_interprets_current_context(
    digit, quote, heard, allowed
):
    assert account_menu_option(digit, quote, heard) is allowed


def numeric_call():
    return {
        "brief": {
            "actions": [VERIFY_ACTION],
            "disclosures": ["account_number"],
            "facts": [{"name": "account_number", "value": "804217"}],
        }
    }


@pytest.mark.parametrize(
    "quote",
    [
        "Say or enter your Example Rewards number.",
        "For security, key in your Harbor Rewards identifier.",
        "Please use the digits on your membership card.",
        "Your loyalty ID, please; the keypad is available.",
        "Type your frequent traveller ID now.",
        "Your ID?",
    ],
)
def test_voice_selects_identifier_by_meaning_without_brand_or_label_registry(quote):
    call = numeric_call()
    # The controller validates selected data, not the model's interpretation.
    assert verification_digits(call, "account_number", quote, quote) == "804217"


def test_fact_names_are_data_and_not_a_hardcoded_type_list():
    call = numeric_call()
    call["brief"]["facts"] = [{"name": "customer_reference", "value": "004217"}]
    call["brief"]["disclosures"] = ["customer_reference"]
    quote = "Please key in the reference printed on your card."
    assert verification_digits(call, "customer_reference", quote, quote) == "004217"


@pytest.mark.parametrize(
    "value,selected,expected",
    [
        ("84 Example St, Unit 2, Test City, MA 02446", "02446", "02446"),
        ("Reference: 004217; group 39", "004217", "004217"),
        ("District 7, code 00123-4567", "00123", "00123"),
    ],
)
def test_keypad_can_select_exact_numeric_span_from_disclosed_text(value, selected, expected):
    call = numeric_call()
    call["brief"]["facts"][0]["value"] = value
    quote = "Please enter the requested reference."
    assert (
        verification_digits(call, "account_number", quote, quote, fact_value=selected) == expected
    )


@pytest.mark.parametrize("selected", ["2446", "8402446", "99", "02 446", "02446#"])
def test_keypad_rejects_partial_joined_absent_or_formatted_numeric_span(selected):
    call = numeric_call()
    call["brief"]["facts"][0]["value"] = "84 Example St, MA 02446"
    quote = "Please enter the requested reference."
    with pytest.raises(Refused, match="exact numeric span"):
        verification_digits(call, "account_number", quote, quote, fact_value=selected)


def test_keypad_span_still_requires_disclosed_fact_and_current_recipient_request():
    call = numeric_call()
    call["brief"]["facts"][0]["value"] = "Address in MA 02446"
    quote = "Enter the postal code."
    with pytest.raises(Refused, match="no current unanswered recipient context"):
        verification_digits(call, "account_number", quote, "", fact_value="02446")
    with pytest.raises(Refused, match="not disclosed"):
        verification_digits(call, "hidden", quote, quote, fact_value="02446")


@pytest.mark.parametrize("sources", [False, True])
def test_account_profile_uses_shared_source_tools_when_enabled(service, brief, sources):
    pytest.importorskip("pipecat")
    from dots_phone.task_voice import create_task_voice

    prepared, _ = account_call(service, brief, sources=sources)
    service.settings.pipecat_engine = "realtime"
    call = service.call("owner", prepared["call_id"])
    llm, context, prompt = create_task_voice(service, call, api_key="offline-fixture")
    assert {tool.name for tool in context.tools.standard_tools} == {
        "finish_call",
        "hang_up",
        "wait_for_recipient",
        *(
            {"search_call_sources", "read_call_source", "request_call_information"}
            if sources
            else set()
        ),
    }
    assert llm._context is context
    assert llm._settings.session_properties.reasoning.effort == "medium"
    assert ("no live source tools" in prompt) is not sources
    assert "gpt-realtime-2.1" == llm._settings.model


@pytest.mark.parametrize(
    "operation",
    [
        "menu",
        "verification",
        "program_verification",
        "source_verification",
        "reference_verification",
        "brief_span_verification",
        "segmented_verification",
        "birthday_verification",
    ],
)
@pytest.mark.parametrize("steered", [False, True])
async def test_account_input_preserves_voice_and_uses_existing_carrier_call(
    service, brief, operation, steered
):
    pytest.importorskip("pipecat")
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock
    from xml.etree import ElementTree

    from dots_phone.carrier_stream import CarrierStream
    from dots_phone.pipecat_voice import PipecatTelephony
    from tests.test_carrier_stream import Socket

    fact = brief.facts[0].model_copy(update={"name": "account_number", "value": "804217"})
    birthday = brief.facts[0].model_copy(update={"name": "birth_date_mmdd", "value": "0710"})
    program = brief.facts[0].model_copy(
        update={"name": "account_program", "value": "Example Rewards"}
    )
    address = brief.facts[0].model_copy(
        update={"name": "contact_location", "value": "84 Example St, Apt 2, MA 02446"}
    )
    brief = brief.model_copy(
        update={
            "facts": [fact, program, address, birthday],
            "disclosures": [
                "account_number",
                "account_program",
                "contact_location",
                "birth_date_mmdd",
            ],
        }
    )
    prepared, worker = account_call(service, brief, sources=operation == "source_verification")
    cid = prepared["call_id"]
    await subscribed(service, cid)
    authorization = service.approve("owner", cid)
    service.settings.dry_run = False
    service.settings.pipecat_account_dtmf = True
    service.start("owner", Start(call_id=cid, revision=1, authorization_id=authorization))
    await service.tick()
    if steered:
        await service.steer(
            "owner",
            Steer(call_id=cid, revision=1, instruction="Continue toward account assistance."),
        )
    call = service.call("owner", cid)
    if operation == "source_verification":
        await relay_source(service, call, worker)
        call = service.call("owner", cid)
    sid = call["provider_sid"]
    stream = CarrierStream(Socket(), "MZfirst")
    quote = {
        "menu": "For account information press two.",
        "verification": "Enter your account number followed by pound.",
        "program_verification": "For identification purposes, please say or enter your Example Rewards number or the phone number associated with your account.",
        "source_verification": "For security, please key in your profile's ZIP.",
        "reference_verification": "Identification purposes. Please enter your member number, followed by the pound key.",
        "brief_span_verification": "Please enter your postal code.",
        "segmented_verification": "To verify your identity, please enter your account number.",
        "birthday_verification": "To verify your identity, please enter your birthdate using two digits for the month and two digits for the day.",
    }[operation]
    llm = SimpleNamespace(dialog=[{"role": "user", "text": quote}], clear_playback=AsyncMock())
    if operation == "menu":
        llm.dialog.append({"role": "user", "text": ""})
    if operation in {"segmented_verification", "birthday_verification"}:
        llm.dialog.append(
            {
                "role": "user",
                "text": "If you prefer not to provide this info, please hold to speak to a crew member.",
            }
        )
    if operation == "birthday_verification":
        # The model can paraphrase a multi-segment prompt, including birthdate.
        quote = "Please enter your birth date, month and day."
    if operation == "menu":
        quote = "Account details are option 2."
    session = SimpleNamespace(
        carrier=stream, ending=False, llm=llm, menu_lock=asyncio.Lock(), used_menus=set()
    )
    client = MagicMock()
    provider = PipecatTelephony(service.settings, service, client)
    provider.sessions[sid] = session
    accepted = []

    def update(**kwargs):
        accepted.append(ElementTree.fromstring(kwargs["twiml"]))
        return SimpleNamespace(sid=sid)

    client.calls.return_value.update.side_effect = update

    async def reconnect(control_index=0):
        while len(accepted) <= control_index:
            await asyncio.sleep(0.001)
        parameters = {
            p.attrib["name"]: p.attrib["value"]
            for p in accepted[control_index].findall("./Connect/Stream/Parameter")
        }
        await stream.attach(
            Socket(), f"MZreplacement{control_index}", parameters["dots_reattach_nonce"]
        )

    receiver = asyncio.create_task(reconnect())
    try:
        if operation == "menu":
            result = await provider.press_phone_key(cid, "2", quote)
        elif operation == "source_verification":
            result = await provider.enter_verification_fact(
                cid, "", quote, source_id="message-a", source_value="02446"
            )
        elif operation == "brief_span_verification":
            result = await provider.enter_verification_fact(
                cid, "contact_location", quote, fact_value="02446"
            )
        elif operation == "birthday_verification":
            result = await provider.enter_verification_fact(cid, "birth_date_mmdd", quote)
        elif operation == "reference_verification":
            from dots_phone.call_task import recipient_evidence

            reference = recipient_evidence(llm.dialog)[0]["id"]
            result = await provider.enter_verification_fact(
                cid, "account_number", prompt_evidence_id=reference, terminator="#"
            )
        else:
            result = await provider.enter_verification_fact(
                cid, "account_number", quote, terminator="#" if operation == "verification" else ""
            )
        await receiver
        assert result["audio_reconnected"] and session.llm is llm
        assert (
            accepted[0].find("Play").attrib["digits"]
            == {
                "menu": "2",
                "verification": "804217#",
                "program_verification": "804217",
                "source_verification": "02446",
                "reference_verification": "804217#",
                "brief_span_verification": "02446",
                "segmented_verification": "804217",
                "birthday_verification": "0710",
            }[operation]
        )
        assert stream.stream_count == 2
        client.calls.create.assert_not_called()
        assert client.calls.call_args.args == (sid,)
        assert "804217" not in json.dumps(service.call("owner", cid)["usage"])
        audited = service.call("owner", cid)
        command = next(iter(audited["private_keypad_commands"].values()))
        assert command["digits"] == accepted[0].find("Play").attrib["digits"]
        assert command["carrier_accepted"] is True
        assert command["recipient_delivery_confirmed"] is False
        assert command["recipient_context"] == "\n".join(
            entry["text"] for entry in llm.dialog if entry["text"].strip()
        )
        assert command["prompt_quote"] == command["recipient_context"]
        assert command["model_prompt_description"] == (
            quote if operation != "reference_verification" else ""
        )
        assert "private_keypad_commands" not in service.get("owner", cid)
        if operation == "birthday_verification":
            assert command["fact_name"] == "birth_date_mmdd"
            assert command["digits"] == "0710"
        with pytest.raises(Refused):
            if operation == "menu":
                await provider.press_phone_key(cid, "2", quote)
            elif operation == "source_verification":
                await provider.enter_verification_fact(
                    cid, "", quote, source_id="message-a", source_value="02446"
                )
            elif operation == "brief_span_verification":
                await provider.enter_verification_fact(
                    cid, "contact_location", quote, fact_value="02446"
                )
            elif operation == "birthday_verification":
                await provider.enter_verification_fact(cid, "birth_date_mmdd", quote)
            else:
                await provider.enter_verification_fact(
                    cid,
                    "account_number",
                    quote,
                    terminator="#" if operation == "verification" else "",
                )
        assert client.calls.return_value.update.call_count == 1
        if operation == "birthday_verification":
            # A fresh recipient request may intentionally ask for the same fact.
            # The earlier attempt is never retried without fresh input.
            llm.dialog.append({"role": "user", "text": "Please try your birthdate again."})
            receiver = asyncio.create_task(reconnect(1))
            await provider.enter_verification_fact(cid, "birth_date_mmdd")
            await receiver
            assert len(accepted) == 2
            assert accepted[1].find("Play").attrib["digits"] == "0710"
    finally:
        receiver.cancel()
        await stream.close()


@pytest.mark.parametrize(
    "change",
    [
        "revision",
        "started_revision",
        "control_revision",
        "grant_revision",
        "grant_hash",
        "grant_consumed",
    ],
)
async def test_account_scope_rechecks_exact_consumed_grant(service, brief, change):
    prepared, worker = account_call(service, brief)
    cid = prepared["call_id"]
    await subscribed(service, cid)
    authorization = service.approve("owner", cid)
    service.settings.dry_run = False
    service.start("owner", Start(call_id=cid, revision=1, authorization_id=authorization))
    await service.tick()
    call = service.call("owner", cid)
    assert worker.scope(call)
    if change.startswith("grant_"):
        grant = service.store.get("approval", authorization, "owner")
        field = change.removeprefix("grant_")
        grant[field] = {"revision": 2, "hash": "0" * 64, "consumed": False}[field]
        service.store.put("approval", authorization, "owner", grant)
    else:
        call[change] += 1
    with pytest.raises(Refused, match="start grant"):
        worker.scope(call)


@pytest.mark.parametrize("legacy", [False, True])
async def test_steering_keeps_original_account_scope_and_invalidates_old_decisions(
    service, brief, legacy
):
    prepared, worker = account_call(service, brief)
    cid = prepared["call_id"]
    await subscribed(service, cid)
    authorization = service.approve("owner", cid)
    service.settings.dry_run = False
    service.start("owner", Start(call_id=cid, revision=1, authorization_id=authorization))
    await service.tick()
    call = service.call("owner", cid)
    if legacy:
        call.pop("started_revision")
        call.pop("control_revision")
        service.save(call)
    grant = service.store.get("approval", authorization, "owner")
    original_brief = json.dumps(call["brief"], sort_keys=True)
    pending = service.pending_decision(cid, "old-question", "Which account detail?")
    for revision in [1, 2]:
        await service.steer(
            "owner",
            Steer(call_id=cid, revision=revision, instruction="Continue to account assistance."),
        )
        call = service.call("owner", cid)
        assert worker.scope(call)
        assert call["started_revision"] == 1
        assert call["revision"] == call["control_revision"] == revision + 1
        assert call["pending"] is None
        assert json.dumps(call["brief"], sort_keys=True) == original_brief
        assert service.store.get("approval", authorization, "owner") == grant
    with pytest.raises(Refused, match="stale"):
        await service.decision(
            "owner",
            Decision(
                call_id=cid, request_id=pending["request_id"], revision=1, answer="Old answer."
            ),
        )
    assert len(service.provider.dials) == 1


async def test_unauthorized_revision_cannot_be_legitimized_by_steering(service, brief):
    prepared, worker = account_call(service, brief)
    cid = prepared["call_id"]
    await subscribed(service, cid)
    authorization = service.approve("owner", cid)
    service.settings.dry_run = False
    service.start("owner", Start(call_id=cid, revision=1, authorization_id=authorization))
    await service.tick()
    call = service.call("owner", cid)
    call["revision"] += 1
    service.save(call)
    with pytest.raises(Refused, match="stale"):
        await service.steer("owner", Steer(call_id=cid, revision=2, instruction="Continue."))
    with pytest.raises(Refused, match="start grant"):
        worker.scope(service.call("owner", cid))
    assert not service.provider.updates


async def test_failed_steering_does_not_advance_account_authorization(service, brief):
    from unittest.mock import AsyncMock

    prepared, worker = account_call(service, brief)
    cid = prepared["call_id"]
    await subscribed(service, cid)
    authorization = service.approve("owner", cid)
    service.settings.dry_run = False
    service.start("owner", Start(call_id=cid, revision=1, authorization_id=authorization))
    await service.tick()
    service.provider.steer = AsyncMock(side_effect=ConnectionError("offline failure"))
    with pytest.raises(ConnectionError):
        await service.steer("owner", Steer(call_id=cid, revision=1, instruction="Continue."))
    call = service.call("owner", cid)
    assert call["revision"] == call["control_revision"] == call["started_revision"] == 1
    assert worker.scope(call)


@pytest.mark.parametrize("sources", [False, True])
async def test_carrier_builder_registers_account_tools_once_with_actual_sdk(
    service, brief, monkeypatch, sources
):
    pytest.importorskip("pipecat")
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from pydantic import SecretStr

    import dots_phone.pipecat_voice as voice
    from dots_phone.pipecat_realtime import SupervisedRealtimeService
    from tests.test_carrier_stream import Socket

    prepared, _ = account_call(service, brief, sources=sources)
    cid = prepared["call_id"]
    await subscribed(service, cid)
    service.settings.dry_run = False
    service.settings.pipecat_account_dtmf = True
    service.settings.twilio_account = SecretStr("AC" + "2" * 32)
    authorization = service.approve("owner", cid)
    service.start("owner", Start(call_id=cid, revision=1, authorization_id=authorization))
    await service.tick()
    call = service.call("owner", cid)
    ws = Socket()
    ws.accept = AsyncMock()
    ws.receive_json = AsyncMock(
        return_value={
            "event": "start",
            "start": {
                "accountSid": service.settings.twilio_account.get_secret_value(),
                "callSid": call["provider_sid"],
                "streamSid": "MZfixture",
                "customParameters": {"dots_call_id": cid},
            },
        }
    )
    initialized, streams = [], []
    original = SupervisedRealtimeService.prepare_listen_first
    carrier = voice.CarrierStream

    def prepare_once(llm, context):
        initialized.append(llm)
        return original(llm, context)

    def capture_stream(*args):
        result = carrier(*args)
        streams.append(result)
        return result

    class Built(Exception):
        pass

    def inspect_context(context):
        assert len(initialized) == 1
        llm = initialized[0]
        names = {tool.name for tool in context.tools.standard_tools}
        assert names == {
            "finish_call",
            "hang_up",
            "wait_for_recipient",
            "press_phone_key",
            "enter_verification_fact",
            *(
                {"search_call_sources", "read_call_source", "request_call_information"}
                if sources
                else set()
            ),
        }
        assert llm._context is context
        assert llm._settings.session_properties.tools is context.tools
        assert all(llm.has_function(name) for name in names)
        from pipecat.adapters.services.open_ai_realtime_adapter import OpenAIRealtimeLLMAdapter

        schema = OpenAIRealtimeLLMAdapter().to_provider_tools_format(context.tools)
        verification = next(t for t in schema if t["name"] == "enter_verification_fact")
        assert verification["parameters"]["properties"]["prompt_evidence_id"]["type"] == "string"
        assert "prompt_quote" not in verification["parameters"]["required"]
        assert "fact_name" in verification["parameters"]["required"]
        if sources:
            search = next(t for t in schema if t["name"] == "search_call_sources")
            assert search["parameters"]["properties"]["provider"]["enum"] == [
                "all",
                "gmail",
                "yahoo",
            ]
        raise Built()

    monkeypatch.setattr(SupervisedRealtimeService, "prepare_listen_first", prepare_once)
    monkeypatch.setattr(voice, "CarrierStream", capture_stream)
    monkeypatch.setattr(voice, "LLMContextAggregatorPair", inspect_context)
    client = MagicMock()
    provider = voice.PipecatTelephony(service.settings, service, client)
    try:
        with pytest.raises(Built):
            await provider.media(ws)
        client.calls.create.assert_not_called()
        client.calls.return_value.update.assert_not_called()
        llm = initialized[0]
        # The SDK rebuilds instructions when tools synchronize. A raw settings
        # mutation can appear correct locally and disappear on the actual wire.
        llm._sync_registered_tool_handlers(llm._context.tools)
        llm.send_client_event = AsyncMock()
        await llm._send_session_update()
        instructions = llm.send_client_event.call_args.args[0].session.instructions
        assert "Perform keypad entry and tool-argument corrections silently" in instructions
        assert "Stay completely silent through automated greetings" in instructions
        assert "ASYNC TOOLS:" not in instructions
        if sources:
            assert not llm._functions["search_call_sources"].cancel_on_interruption
        for name, arguments in [
            ("press_phone_key", {"digit": "2", "menu_quote": "For balances press two."}),
            ("enter_verification_fact", {"fact_name": "account_number"}),
        ]:
            # Exercise the actual registered SDK handler: an accepted keypad
            # change must listen for carrier speech, not request another turn.
            setattr(provider, name, AsyncMock(return_value={"carrier_control_accepted": True}))
            params = SimpleNamespace(tool_call_id=name, result_callback=AsyncMock())
            await initialized[0]._functions[name].handler.invoke(arguments, params)
            output = llm.send_client_event.call_args.args[0]
            assert output.item.type == "function_call_output"
            assert output.item.call_id == name
            assert params.result_callback.call_args.kwargs["properties"].run_llm is False
            # A local refusal still lets the agent correct its own arguments.
            setattr(provider, name, AsyncMock(side_effect=voice.Refused("quote mismatch")))
            params = SimpleNamespace(tool_call_id=name + "_refused", result_callback=AsyncMock())
            await initialized[0]._functions[name].handler.invoke(arguments, params)
            output = llm.send_client_event.call_args.args[0]
            assert json.loads(output.item.output)["input_status"] == "not_submitted"
            assert params.result_callback.call_args.kwargs["properties"].run_llm is False
            assert params.tool_call_id in llm.source_continuations
            llm.source_continuation_task.cancel()
            await asyncio.gather(llm.source_continuation_task, return_exceptions=True)
            llm.source_continuations.clear()
    finally:
        for stream in streams:
            await stream.close()


def test_verification_uses_exact_data_and_model_selected_controls():
    call = numeric_call()
    quote = "Your member ID, then hash."
    assert verification_digits(call, "account_number", quote, quote, terminator="#") == "804217#"
    assert verification_digits(call, "account_number", quote, quote) == "804217"
    quote = "Key in just the last four digits from your card."
    assert (
        verification_digits(call, "account_number", quote, quote, digit_offset=2, digit_count=4)
        == "4217"
    )


def test_menu_context_can_contain_all_options_without_model_copying_a_clause():
    quote = "Press one for flights or two for account information."
    assert account_menu_option("1", "The first option", quote)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"terminator": "12"},
        {"terminator": "#*"},
        {"terminator": ["#"]},
        {"digit_offset": -1},
        {"digit_offset": 6},
        {"digit_offset": True},
        {"digit_count": 0},
        {"digit_count": 7},
        {"digit_count": 1.5},
        {"digit_offset": 3, "digit_count": 4},
    ],
)
def test_verification_cannot_inject_or_exceed_supplied_data(kwargs):
    quote = "Key in the requested digits."
    with pytest.raises(Refused):
        verification_digits(numeric_call(), "account_number", quote, quote, **kwargs)


@pytest.mark.parametrize("value", ["AB804217", "８０４２１７", "804217#", "804217\n", "", "1" * 17])
def test_keypad_requires_an_exact_ascii_numeric_representation(value):
    call = numeric_call()
    call["brief"]["facts"][0]["value"] = value
    quote = "Enter your identifier."
    with pytest.raises(Refused):
        verification_digits(call, "account_number", quote, quote)


@pytest.mark.parametrize("case", ["pin", "undisclosed", "missing", "unheard", "no_action"])
def test_verification_refuses_missing_unapproved_or_unobserved_data(case):
    call = numeric_call()
    quote = "Enter your identifier."
    heard, fact = quote, "account_number"
    if case == "pin":
        fact = "pin"
    elif case == "undisclosed":
        call["brief"]["disclosures"] = []
    elif case == "missing":
        call["brief"]["facts"] = []
    elif case == "unheard":
        heard = ""
    else:
        call["brief"]["actions"] = []
    with pytest.raises(Refused):
        verification_digits(call, fact, quote, heard)


def source_pending(service, cid, operation, **arguments):
    return service.pending_decision(
        cid,
        "source-tool",
        json.dumps({"operation": operation, "arguments": arguments}),
    )


def source_reply(cid, pending, answer, source=None):
    return Decision(
        call_id=cid,
        request_id=pending["request_id"],
        revision=pending["revision"],
        answer=json.dumps(answer),
        action_names=[SOURCE_ACTION],
        source=source,
    )


async def relay_source(service, call, worker):
    cid = call["call_id"]
    pending = source_pending(service, cid, "search", query="profile postal address")
    assert not handles_locally(worker, service.call("owner", cid))
    await service.decision(
        "owner",
        source_reply(
            cid,
            pending,
            {
                "untrusted_data": True,
                "messages": [
                    {
                        "message_id": "message-a",
                        "subject": "Your profile",
                        "snippet": "Profile details",
                    }
                ],
            },
        ),
    )
    pending = source_pending(service, cid, "read", message_id="message-a", focus="ZIP", offset=0)
    source = PreparedSource(
        provider="yahoo",
        source_id="message-a",
        text="The account profile lists ZIP 02446.",
        truncated=False,
        verified_at=now(),
        expires_at=now() + timedelta(minutes=5),
    )
    await service.decision(
        "owner",
        source_reply(
            cid,
            pending,
            {
                "untrusted_data": True,
                "message_id": source.source_id,
                "text": source.text,
            },
            source,
        ),
    )
    return source


async def test_missing_brief_fact_can_be_retrieved_without_rewriting_scope(service, brief):
    prepared, worker = account_call(service, brief, sources=True)
    cid = prepared["call_id"]
    call = service.call("owner", cid)
    call.update(state="active", provider_sid="CAfixture")
    service.save(call)
    original = call["brief"]
    source = await relay_source(service, call, worker)
    current = service.call("owner", cid)
    assert current["brief"] == original and current["brief_hash"] == digest(original)
    assert "postal_code" not in current["brief"]["disclosures"]
    assert len(current["source_requests"]) == 2
    assert source.text in service.provider.updates[-1][1]
    quote = "Key in your postcode, please."
    assert (
        verification_digits(
            current, "", quote, quote, source_id=source.source_id, source_value="02446"
        )
        == "02446"
    )


@pytest.mark.parametrize(
    "case", ["invented", "substring", "expired", "revision", "hash", "no_action", "ambiguous"]
)
async def test_retrieved_keypad_data_requires_exact_current_source_evidence(service, brief, case):
    prepared, worker = account_call(service, brief, sources=True)
    cid = prepared["call_id"]
    call = service.call("owner", cid)
    call.update(state="active", provider_sid="CAfixture")
    service.save(call)
    await relay_source(service, call, worker)
    call = service.call("owner", cid)
    selected, name = "02446", ""
    source = call["retrieved_sources"]["message-a"]
    if case == "invented":
        selected = "90210"
    elif case == "substring":
        selected = "2446"
    elif case == "expired":
        source["expires_at"] = (now() - timedelta(seconds=1)).isoformat()
    elif case == "revision":
        call["revision"] += 1
    elif case == "hash":
        call["brief_hash"] = "0" * 64
    elif case == "no_action":
        call["brief"]["actions"].remove(SOURCE_ACTION)
    else:
        name = "account_number"
    quote = "Please enter the requested digits."
    with pytest.raises(Refused):
        verification_digits(call, name, quote, quote, source_id="message-a", source_value=selected)


@pytest.mark.parametrize(
    "case",
    [
        "unsearched",
        "mismatch",
        "stale",
        "scope",
        "wrong_provider",
        "future",
        "late_expiry",
        "actions",
        "conclusion",
    ],
)
async def test_source_worker_cannot_supply_unbound_or_stale_reads(service, brief, case):
    prepared, worker = account_call(service, brief, sources=True)
    cid = prepared["call_id"]
    call = service.call("owner", cid)
    call.update(state="active", provider_sid="CAfixture", source_inventory=["message-a"])
    if case == "unsearched":
        call["source_inventory"] = []
    service.save(call)
    pending = source_pending(service, cid, "read", message_id="message-a", focus="", offset=0)
    source = PreparedSource(
        provider="gmail",
        source_id="message-a",
        text="Profile ZIP 02446.",
        truncated=False,
        verified_at=now(),
        expires_at=now() + timedelta(minutes=5),
    )
    answer = {"untrusted_data": True, "message_id": "message-a", "text": source.text}
    reply = source_reply(cid, pending, answer, source)
    if case == "mismatch":
        reply.answer = json.dumps({**answer, "text": "ZIP 90210"})
    elif case == "stale":
        reply.source.expires_at = now() - timedelta(seconds=1)
    elif case == "future":
        reply.source.verified_at = now() + timedelta(seconds=10)
    elif case == "late_expiry":
        reply.source.expires_at = now() + timedelta(days=1)
    elif case == "wrong_provider":
        reply.source.provider = "owned_fixture"
    elif case == "scope":
        reply.source.source_id = "another-message"
    elif case == "actions":
        reply.action_names.append(VERIFY_ACTION)
    elif case == "conclusion":
        from dots_phone.models import Conclusion

        reply.conclusion = Conclusion(outcome="unresolved", unresolved_questions=["Still checking"])
    with pytest.raises(Refused):
        await service.decision("owner", reply)
    current = service.call("owner", cid)
    assert current["pending"]["request_id"] == pending["request_id"]
    assert not current.get("retrieved_sources") and not service.provider.updates


def test_scope_does_not_depend_on_constraint_punctuation(service, brief):
    prepared, worker = account_call(service, brief)
    call = service.call("owner", prepared["call_id"])
    call["brief"]["constraints"] = [ACCOUNT_CONSTRAINT + "."]
    call["brief_hash"] = digest(call["brief"])
    assert worker.scope(call)


def test_keypad_refusal_distinguishes_controller_failure_from_prior_input(service, brief):
    pytest.importorskip("pipecat")
    from unittest.mock import MagicMock

    from dots_phone.pipecat_voice import KeypadRefused, PipecatTelephony

    prepared, _ = account_call(service, brief)
    provider = PipecatTelephony(service.settings, service, MagicMock())
    result = provider.keypad_refusal(
        prepared["call_id"], "verification", KeypadRefused("quote not observed")
    )
    assert result["input_status"] == "not_submitted"
    assert result["reason"] == "quote not observed"
    result = provider.keypad_refusal(
        prepared["call_id"],
        "verification",
        KeypadRefused("already attempted", input_status="previous_attempt"),
    )
    assert result["input_status"] == "previous_attempt"
    call = service.call("owner", prepared["call_id"])
    assert len(call["usage"]["keypad_refusals"]) == 2
    assert not call["usage"].get("carrier_menu_controls")


def test_menu_refusal_supplies_current_context_without_verbatim_repair(service, brief):
    pytest.importorskip("pipecat")
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from dots_phone.pipecat_voice import KeypadRefused, PipecatTelephony

    prepared, _ = account_call(service, brief)
    call = service.call("owner", prepared["call_id"])
    provider = PipecatTelephony(service.settings, service, MagicMock())
    provider.sessions[call["provider_sid"]] = SimpleNamespace(
        llm=SimpleNamespace(
            dialog=[
                {"role": "user", "text": "Press one for yes or two for no."},
                {"role": "user", "text": ""},
            ]
        )
    )
    result = provider.keypad_refusal(call["call_id"], "menu", KeypadRefused("quote not observed"))
    assert result["observed_menu"] == "Press one for yes or two for no."
    assert "attaches current recipient context automatically" in result["guidance"]
    assert not result["carrier_control_accepted"]
    assert not service.call("owner", call["call_id"])["usage"].get("carrier_menu_controls")


def test_keypad_quote_refusal_supplies_only_current_observed_recipient_evidence(service, brief):
    pytest.importorskip("pipecat")
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from dots_phone.pipecat_voice import KeypadRefused, PipecatTelephony

    prepared, _ = account_call(service, brief)
    call = service.call("owner", prepared["call_id"])
    call["provider_sid"] = "CAfixture"
    service.save(call)
    provider = PipecatTelephony(service.settings, service, MagicMock())
    current = "Identification purposes. Please enter your member number, followed by pound."
    provider.sessions["CAfixture"] = SimpleNamespace(
        llm=SimpleNamespace(
            dialog=[
                {"role": "user", "text": "An earlier unrelated prompt."},
                {"role": "assistant", "text": "My previous response."},
                {"role": "user", "text": current},
            ]
        )
    )
    feedback = provider.keypad_refusal(
        call["call_id"], "verification", KeypadRefused("quote not observed")
    )
    expected_context = "An earlier unrelated prompt.\n" + current
    assert feedback["recipient_context"] == expected_context
    assert feedback["recipient_evidence"] == recipient_evidence(
        [{"role": "user", "text": expected_context}]
    )
    assert "no exact quote or evidence ID is needed" in feedback["guidance"]
    assert not call["usage"].get("carrier_menu_controls")


@pytest.mark.parametrize("defect", ["stale", "mixed", "invented", "missing"])
def test_legacy_verification_prompt_hints_cannot_override_server_context(defect):
    from dots_phone.account_task import verification_prompt_quote

    observed = "Enter your member number followed by pound."
    reference = recipient_evidence([{"role": "user", "text": observed}])[0]["id"]
    quote = ""
    if defect == "stale":
        observed = "Now enter the last two digits only."
    elif defect == "mixed":
        quote = observed
    elif defect == "invented":
        reference = "0" * 16
    else:
        reference = ""
    assert verification_prompt_quote(quote, observed, reference) == observed


def test_authenticated_mcp_routes_account_sources_but_keeps_reports_local(settings, brief):
    from fastapi.testclient import TestClient

    from dots_phone.server import create_app
    from tests.conftest import FakeProvider, FakeSender

    app = create_app(settings, FakeProvider(), FakeSender())
    with TestClient(app) as client:
        service = app.state.service
        prepared, _ = account_call(service, brief, sources=True)
        cid = prepared["call_id"]
        call = service.call("owner", cid)
        call.update(state="active", provider_sid="CAfixture", dial_started=now().timestamp())
        service.save(call)
        # The real queued->dialing->active path always supplies dial_started.
        # Exercise maintenance now, rather than racing the background tick.
        client.portal.call(service.tick)
        token = app.state.oauth.issue("fixture-client").access_token
        pending = source_pending(service, cid, "search", query="profile")
        reply = source_reply(
            cid,
            pending,
            {
                "untrusted_data": True,
                "messages": [
                    {"message_id": "message-a", "subject": "Profile", "snippet": "Profile details"}
                ],
            },
        )

        def submit(reply):
            response = client.post(
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
                        # The client uses only the original native argument schema.
                        "arguments": reply.model_dump(mode="json", exclude={"source"}),
                        "_meta": {
                            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                            "io.modelcontextprotocol/clientCapabilities": {},
                        },
                    },
                },
            )
            assert response.status_code == 200
            return response.json()["result"]

        assert not submit(reply).get("isError", False)
        assert service.call("owner", cid)["state"] == "active"
        pending = source_pending(service, cid, "read", message_id="message-a", focus="", offset=0)
        original = service.call("owner", cid)["brief_hash"]
        reply = source_reply(
            cid,
            pending,
            {
                "untrusted_data": True,
                "message_id": "message-a",
                "text": "Profile ZIP 02446.",
                "source": {
                    "provider": "gmail",
                    "truncated": False,
                    "verified_at": now().isoformat(),
                    "expires_at": (now() + timedelta(minutes=5)).isoformat(),
                },
            },
        )
        assert not submit(reply).get("isError", False)
        current = service.call("owner", cid)
        assert current["retrieved_sources"]["message-a"]["text"] == "Profile ZIP 02446."
        assert current["brief_hash"] == original
        pending = service.pending_decision(
            cid, "report", json.dumps({"operation": "complete", "arguments": {"report": {}}})
        )
        result = submit(source_reply(cid, pending, {"untrusted_data": True, "messages": []}))
        assert result["isError"]
        assert "assigned to the local subscription worker" in result["content"][0]["text"]


async def test_realtime_source_request_reaches_external_worker_without_local_interference(
    service, brief
):
    pytest.importorskip("pipecat")
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from dots_phone.pipecat_realtime import SupervisedRealtimeService
    from dots_phone.pipecat_voice import PipecatTelephony

    prepared, worker = account_call(service, brief, sources=True)
    cid = prepared["call_id"]
    call = service.call("owner", cid)
    call.update(state="active", provider_sid="CAfixture")
    service.save(call)
    worker.answer = AsyncMock(side_effect=AssertionError("source must not go to local reporter"))
    llm = SupervisedRealtimeService(supervisor=service, call_id=cid, api_key="offline-fixture")
    llm.dialog = [
        {"role": "user", "text": "Earlier context " * 300},
        {"role": "user", "text": "Could you confirm your home mailing address, including ZIP?"},
    ]
    llm._websocket = SimpleNamespace(send=AsyncMock())
    llm._create_response = AsyncMock()
    llm._truncate_current_audio_response = AsyncMock()
    llm.clear_playback = AsyncMock()
    provider = PipecatTelephony(service.settings, service, MagicMock())
    provider.sessions["CAfixture"] = SimpleNamespace(llm=llm)
    service.provider = provider
    params = SimpleNamespace(tool_call_id="lookup", result_callback=AsyncMock())
    lookup = asyncio.create_task(
        llm.request_supervision(
            params,
            json.dumps({"operation": "search", "arguments": {"query": "profile ZIP"}}),
            defer_source_speech=True,
        )
    )
    try:
        await asyncio.sleep(0)
        service.settings.call_state_wait_seconds = 15
        state = await asyncio.wait_for(service.watch("owner", cid), timeout=0.2)
        assert state["state"] == "awaiting_decision"
        assert (
            state["pending"]["context"]["recent_transcript"][-1]["text"] == llm.dialog[-1]["text"]
        )
        assert state["pending"]["context"]["goal"] == call["brief"]["purpose"]
        assert state["pending"]["context"]["untrusted_data"] is True
        await service.decision(
            "owner", source_reply(cid, state["pending"], {"untrusted_data": True, "messages": []})
        )
        await asyncio.wait_for(lookup, timeout=0.2)
        worker.answer.assert_not_awaited()
        params.result_callback.assert_awaited_once()
        assert "untrusted_data" in llm._websocket.send.call_args.args[0]
        # The Realtime function output wraps the supervisor's JSON answer as data.
        wire = json.loads(llm._websocket.send.call_args.args[0])
        delivered = json.loads(wire["item"]["output"])
        while isinstance(delivered, str):
            delivered = json.loads(delivered)
        assert json.loads(delivered["supervisor_answer"])["coverage_known"] is False
    finally:
        if not lookup.done():
            lookup.cancel()
        await asyncio.gather(lookup, return_exceptions=True)
        if llm.source_continuation_task:
            llm.source_continuation_task.cancel()
            await asyncio.gather(llm.source_continuation_task, return_exceptions=True)


@pytest.mark.parametrize("operation", ["search", "read"])
async def test_source_failure_is_returned_as_failure_not_an_empty_lookup(service, brief, operation):
    prepared, _ = account_call(service, brief, sources=True)
    cid = prepared["call_id"]
    call = service.call("owner", cid)
    call.update(state="active", provider_sid="CAfixture")
    service.save(call)
    arguments = (
        {"query": "profile"}
        if operation == "search"
        else {"message_id": "message-a", "focus": "", "offset": 0}
    )
    pending = source_pending(service, cid, operation, **arguments)
    error = {"untrusted_data": True, "error": "The connected source is temporarily unavailable."}
    await service.decision("owner", source_reply(cid, pending, error))
    current = service.call("owner", cid)
    assert current["state"] == "active" and not current.get("retrieved_sources")
    assert current["source_requests"][-1]["source_error"]
    assert json.loads(service.provider.updates[-1][1]) == error


def coverage(query, provider="gmail", status="ok"):
    return {"provider": provider, "query": query, "status": status, "more_available": False}


@pytest.mark.parametrize(
    "defect",
    ["missing", "wrong_query", "wrong_provider", "duplicate", "partial", "failed_with_message"],
)
async def test_provider_selected_search_requires_matching_coverage(service, brief, defect):
    prepared, _ = account_call(service, brief, sources=True)
    cid = prepared["call_id"]
    call = service.call("owner", cid)
    call.update(state="active", provider_sid="CAfixture")
    service.save(call)
    pending = source_pending(service, cid, "search", query="profile", provider="gmail")
    output = {"untrusted_data": True, "searches": [coverage("profile")], "messages": []}
    if defect == "missing":
        output.pop("searches")
    elif defect == "wrong_query":
        output["searches"][0]["query"] = "silently rewritten"
    elif defect == "wrong_provider":
        output["searches"][0]["provider"] = "yahoo"
    elif defect == "duplicate":
        output["searches"].append(coverage("profile"))
    elif defect == "partial":
        call = service.call("owner", cid)
        call["pending"]["question"] = json.dumps(
            {"operation": "search", "arguments": {"query": "profile", "provider": "all"}}
        )
        service.save(call)
    else:
        output["searches"][0]["status"] = "error"
        output["messages"] = [{"message_id": "invented", "provider": "gmail"}]
    with pytest.raises(Refused):
        await service.decision("owner", source_reply(cid, pending, output))
    assert not service.provider.updates


@pytest.mark.parametrize("case", ["empty_first", "unrelated_person", "missing"])
async def test_source_recovery_replay_preserves_evidence_scope_and_final_report(
    service, brief, case
):
    """Scripted transport replay, not proof that the live model chooses these steps."""
    prepared, worker = account_call(service, brief, sources=True)
    cid = prepared["call_id"]
    call = service.call("owner", cid)
    call.update(state="active", provider_sid="CAfixture")
    service.save(call)
    frozen = (call["brief"], call["brief_hash"], call.get("authorization_id"))
    first_messages = (
        [{"message_id": "other-person", "provider": "gmail", "subject": "Forwarded itinerary"}]
        if case == "unrelated_person"
        else []
    )

    async def search(query, messages):
        pending = source_pending(service, cid, "search", query=query, provider="gmail")
        await service.decision(
            "owner",
            source_reply(
                cid,
                pending,
                {"untrusted_data": True, "searches": [coverage(query)], "messages": messages},
            ),
        )
        delivered = json.loads(service.provider.updates[-1][1])
        assert delivered["searches"][0]["query"] == query
        assert delivered["coverage_known"] is True

    async def read(mid, text):
        pending = source_pending(service, cid, "read", message_id=mid, focus="", offset=0)
        await service.decision(
            "owner",
            source_reply(
                cid,
                pending,
                {
                    "untrusted_data": True,
                    "message_id": mid,
                    "text": text,
                    "source": {
                        "provider": "gmail",
                        "truncated": False,
                        "verified_at": now().isoformat(),
                        "expires_at": call["brief"]["deadline"],
                    },
                },
            ),
        )

    await search("FictionalProgram", first_messages)
    if first_messages:
        await read(
            "other-person", "Passenger: Morgan Vale. Forwarded by Casey Rowan. Passenger ZIP 90001."
        )
    await search(
        '"Casey Rowan" profile',
        []
        if case == "missing"
        else [{"message_id": "owner-profile", "provider": "gmail", "subject": "Contact profile"}],
    )
    if case != "missing":
        await read("owner-profile", "Account holder Casey Rowan. Mailing ZIP 10001.")
    current = service.call("owner", cid)
    assert (current["brief"], current["brief_hash"], current.get("authorization_id")) == frozen
    queries = [
        r["arguments"]["query"] for r in current["source_requests"] if r["operation"] == "search"
    ]
    assert queries == ["FictionalProgram", '"Casey Rowan" profile']
    if case == "unrelated_person":
        assert "Morgan Vale" in current["retrieved_sources"]["other-person"]["text"]
    quote = (
        "I cannot release the balance without verification."
        if case == "missing"
        else "Your available balance is 125 points."
    )
    service.observe_transcript(cid, [{"role": "user", "text": quote}])
    pending = source_pending(
        service,
        cid,
        "complete",
        report={
            "outcome": "unresolved" if case == "missing" else "completed",
            "evidence": [{"criterion_id": "balance", "text": quote, "quote": quote}],
            "unresolved_questions": [
                "Required identity information remains unverified after limited searches."
            ]
            if case == "missing"
            else [],
        },
    )
    await worker.answer(service.call("owner", cid), pending)
    service.terminal(service.call("owner", cid), "completed", "task_finished")
    result = service.result("owner", cid)
    assert result["outcome"] == ("unresolved" if case == "missing" else "completed")
    assert result["confirmed_facts"][0]["quote"] == quote.rstrip(".")
    assert result["source_activity"] == {
        "search_requests": 2,
        "lookup_requests": 0,
        "successful_lookup_requests": 0,
        "read_requests": 2 if case == "unrelated_person" else 0 if case == "missing" else 1,
        "successful_read_requests": 2
        if case == "unrelated_person"
        else 0
        if case == "missing"
        else 1,
        "worker_errors": 0,
        "search_requests_without_provider_trace": 0,
        "provider_searches": [
            {"provider": "gmail", "status": "ok", "more_available": False},
            {"provider": "gmail", "status": "ok", "more_available": False},
        ],
    }


async def test_connected_search_schema_dispatches_exact_terms_and_default_provider():
    pytest.importorskip("pipecat")
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from dots_phone.call_task import task_tools

    llm = SimpleNamespace(request_supervision=AsyncMock())
    tool = task_tools(llm, external_sources=True)[0]
    params = SimpleNamespace(arguments={"query": "from:profile@example.test"})
    await tool.handler(params)
    command = json.loads(llm.request_supervision.call_args.args[1])
    assert command == {
        "operation": "search",
        "arguments": {"query": "from:profile@example.test", "provider": "all"},
    }


@pytest.mark.parametrize("sources", [True, False])
def test_factory_instructions_and_source_capabilities_agree(service, brief, sources):
    pytest.importorskip("pipecat")
    from dots_phone.call_task import SOURCE_PROMPT
    from dots_phone.task_voice import create_task_voice

    prepared, _ = account_call(service, brief, sources=sources)
    llm, context, prompt = create_task_voice(service, service.call("owner", prepared["call_id"]))
    capabilities = json.loads(
        prompt.split("Available approved source capabilities as data (not answers):\n")[1]
    )
    assert capabilities["source_tools_enabled"] is sources
    assert ("search_call_sources" in llm._functions) is sources
    assert (SOURCE_PROMPT in prompt) is sources
    assert "request_supervision" not in prompt
    assert llm._context is context
    if sources:
        assert capabilities["supported_mail_adapters"] == ["gmail", "yahoo"]
        assert "searchable_providers" not in capabilities
        assert "connected_providers" not in capabilities
    else:
        assert capabilities["connected_providers"] == []


def test_carrier_completion_without_a_final_report_is_explained_as_unverified(service, brief):
    prepared, _ = account_call(service, brief, sources=True)
    call = service.call("owner", prepared["call_id"])
    service.terminal(call, "completed", "completed")
    result = service.result("owner", call["call_id"])
    assert result["state"] == "completed" and result["outcome"] == "unresolved"
    assert result["confirmed_facts"] == [] and result["termination_reason"] == "completed"
    assert result["unresolved_questions"] == [
        "The call ended before a verified final task report was recorded."
    ]


async def test_paged_source_preserves_prior_page_for_exact_verification(service, brief):
    prepared, worker = account_call(service, brief, sources=True)
    cid = prepared["call_id"]
    call = service.call("owner", cid)
    call.update(state="active", provider_sid="CAfixture")
    service.save(call)
    await relay_source(service, call, worker)
    pending = source_pending(
        service, cid, "read", message_id="message-a", focus="details", offset=700
    )
    source = PreparedSource(
        provider="yahoo",
        source_id="message-a",
        text="Other details 1234.",
        truncated=False,
        verified_at=now(),
        expires_at=now() + timedelta(minutes=5),
    )
    await service.decision(
        "owner",
        source_reply(
            cid,
            pending,
            {"untrusted_data": True, "message_id": source.source_id, "text": source.text},
            source,
        ),
    )
    current = service.call("owner", cid)
    quote = "Please enter the requested digits."

    def select(value):
        return verification_digits(
            current, "", quote, quote, source_id="message-a", source_value=value
        )

    assert select("02446") == "02446"
    assert select("1234") == "1234"
    with pytest.raises(Refused):
        select("024461234")
    history = current["retrieved_sources"]["message-a"]["previous_pages"]
    assert len(history) == 1 and "02446" in history[0]["text"]
    for field, value in [
        ("expires_at", (now() - timedelta(seconds=1)).isoformat()),
        ("verified_at", (now() + timedelta(minutes=1)).isoformat()),
        ("revision", 0),
        ("brief_hash", "0" * 64),
        ("provider", "gmail"),
    ]:
        original = history[0][field]
        history[0][field] = value
        with pytest.raises(Refused):
            select("02446")
        history[0][field] = original
