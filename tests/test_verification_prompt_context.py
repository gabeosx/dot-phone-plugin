"""Offline boundaries for the current, unanswered IVR request."""

from types import SimpleNamespace

import pytest

pytest.importorskip("pipecat")

from dots_phone.account_task import verification_prompt_quote
from dots_phone.call_task import recipient_evidence
from dots_phone.pipecat_voice import verification_prompt_context


def session(*entries, start=0):
    return SimpleNamespace(
        llm=SimpleNamespace(dialog=list(entries)),
        keypad_dialog_anchor=entries[start - 1] if start else None,
    )


def user(text):
    return {"role": "user", "text": text}


def test_split_request_and_followup_remain_one_unanswered_context():
    prompt = (
        "Please enter your birthdate using two digits for the month and two digits for the day."
    )
    followup = "If you prefer not to provide this info, please hold to speak to a crew member."
    s = session(user(prompt), user(followup), user(""))
    context = verification_prompt_context(s)
    assert prompt in context and followup in context
    reference = recipient_evidence([user(context)])[0]["id"]
    assert verification_prompt_quote("", context, reference)


def test_prior_request_is_retired_after_keypad_attempt():
    old = "Enter your birthdate."
    entries = [user(old)]
    s = session(*entries, user("Please wait."), start=1)
    context = verification_prompt_context(s)
    assert context == "Please wait."
    old_id = recipient_evidence([user(old)])[0]["id"]
    # Legacy descriptions and references cannot replace the server context.
    assert verification_prompt_quote(old, context, old_id) == "Please wait."


def test_filler_or_spoken_clarification_does_not_erase_recipient_request():
    prompt = "Enter your member number."
    s = session(user(prompt), {"role": "assistant", "text": "One sec."})
    assert verification_prompt_context(s) == prompt


def test_late_transcript_for_attempted_audio_turn_cannot_reopen_context():
    s = session()
    s.keypad_audio_turn_anchor = 3
    s.llm.dialog.append({**user("Enter your member number."), "recipient_turn": 3})
    assert verification_prompt_context(s) == ""
    s.llm.dialog.append({**user("Please try that number again."), "recipient_turn": 4})
    assert verification_prompt_context(s) == "Please try that number again."


def test_unanswered_prompt_survives_many_segments_and_excludes_source_or_assistant_text():
    s = session(user("Enter your member number."), *[user("x" * 400) for _ in range(16)])
    assert "Enter your member number." in verification_prompt_context(s)
    assert len(verification_prompt_context(s)) > 4096
    single = "Context " * 800 + "Please enter your member number."
    assert verification_prompt_context(session(user(single))) == single
    assert (
        verification_prompt_context(session({"role": "system", "text": "Invented request"})) == ""
    )


def test_keypad_boundary_survives_the_rolling_dialog_buffer():
    s = session(*[user(str(i)) for i in range(32)], start=32)
    assert verification_prompt_context(s) == ""
    s.llm.dialog = [*s.llm.dialog[-31:], user("New prompt.")]
    assert verification_prompt_context(s) == "New prompt."
    s.llm.dialog = [user("Later prompt.") for _ in range(32)]
    assert "Later prompt." in verification_prompt_context(s)


@pytest.mark.parametrize("operation", ["menu", "verification"])
@pytest.mark.parametrize("response_policy", [None, "direct_with_codex", "always_consult"])
async def test_native_audio_turn_can_bind_keypad_without_asr_and_retires_late_text(
    service, brief, operation, response_policy
):
    import asyncio
    from copy import deepcopy
    from datetime import timedelta
    from unittest.mock import AsyncMock, MagicMock

    from dots_phone.account_task import MENU_ACTION, VERIFY_ACTION
    from dots_phone.models import Start, now
    from dots_phone.pipecat_voice import PipecatTelephony
    from dots_phone.service import Refused, digest
    from tests.conftest import subscribed
    from tests.test_account_task import account_call

    fact = brief.facts[0].model_copy(update={"name": "member", "value": "004217"})
    brief = brief.model_copy(update={"facts": [fact], "disclosures": ["member"]})
    prepared, _ = account_call(service, brief)
    cid = prepared["call_id"]
    await subscribed(service, cid)
    authorization = service.approve("owner", cid)
    service.settings.dry_run = False
    service.settings.pipecat_account_dtmf = True
    service.start("owner", Start(call_id=cid, revision=1, authorization_id=authorization))
    await service.tick()
    call = service.call("owner", cid)
    call["recipient_audio_turn"] = 1
    if response_policy:
        call["runtime_supervision"] = "local_reasoner"
        call["reasoner_response_policy"] = response_policy
    service.save(call)
    sid = call["provider_sid"]
    llm = SimpleNamespace(dialog=[], clear_playback=AsyncMock())
    s = SimpleNamespace(
        carrier=SimpleNamespace(
            begin_change=lambda: "one-use-fixture-nonce",
            wait_change=AsyncMock(),
            stream_count=2,
            close=AsyncMock(),
        ),
        ending=False,
        llm=llm,
        menu_lock=asyncio.Lock(),
        used_menus=set(),
    )
    client = MagicMock()
    client.calls.return_value.update.return_value = SimpleNamespace(sid=sid)
    provider = PipecatTelephony(service.settings, service, client)
    provider.sessions[sid] = s

    async def select():
        if operation == "menu":
            return await provider.press_phone_key(cid, "2")
        return await provider.enter_verification_fact(cid, "member")

    if operation == "verification":
        with pytest.raises(Refused, match="not disclosed"):
            await provider.enter_verification_fact(cid, "unapproved_fact")
        client.calls.return_value.update.assert_not_called()
    result = await select()
    assert result["carrier_control_accepted"]
    command = next(iter(service.call("owner", cid)["private_keypad_commands"].values()))
    assert command["recipient_audio_turn"] == 1
    assert command["recipient_context"] == ""  # Never invent an ASR quote.
    assert command["digits"] == ("2" if operation == "menu" else "004217")
    llm.dialog.append({**user("The transcript arrived late."), "recipient_turn": 1})
    assert verification_prompt_context(s) == ""
    with pytest.raises(Refused, match="already attempted"):
        await select()
    assert client.calls.return_value.update.call_count == 1

    call = service.call("owner", cid)
    call["recipient_audio_turn"] = 2
    service.save(call)
    # A genuinely new audio request can deliberately select the same known fact.
    assert (await select())["carrier_control_accepted"]
    assert client.calls.return_value.update.call_count == 2
    # Every route is bounded by the owner's actual scope and duration/
    # spending authorization, rather than an invented count of keypad requests.
    for recipient_turn in range(3, 10):
        call = service.call("owner", cid)
        call["recipient_audio_turn"] = recipient_turn
        service.save(call)
        assert (await select())["carrier_control_accepted"]
    assert client.calls.return_value.update.call_count == 9
    with pytest.raises(Refused, match="already attempted"):
        await select()

    authorized = service.call("owner", cid)
    grant = service.store.get("approval", authorization, "owner")
    for revoked in (
        "active_call",
        "deadline",
        "duration",
        "spending",
        "action",
        "disclosure",
        "grant",
    ):
        call = deepcopy(authorized)
        current_grant = deepcopy(grant)
        call["recipient_audio_turn"] = 10
        if revoked == "active_call":
            call["state"] = "stopping"
        elif revoked == "deadline":
            call["brief"]["deadline"] = (now() - timedelta(seconds=1)).isoformat()
        elif revoked == "duration":
            call["brief"]["max_seconds"] += 1
        elif revoked == "spending":
            call["brief"]["budget_usd"] = "2.01"
        elif revoked == "action":
            forbidden = MENU_ACTION if operation == "menu" else VERIFY_ACTION
            call["brief"]["actions"].remove(forbidden)
        elif revoked == "disclosure":
            call["brief"]["disclosures"] = []
        else:
            current_grant["consumed"] = False
        call["brief_hash"] = digest(call["brief"])
        service.save(call)
        service.store.put("approval", authorization, "owner", current_grant)
        with pytest.raises(Refused):
            await select()
        assert client.calls.return_value.update.call_count == 9, revoked


def test_native_audio_binding_does_not_authorize_an_undisclosed_fact():
    from dots_phone.account_task import verification_digits
    from dots_phone.service import Refused

    call = {
        "brief": {
            "actions": ["verify_account_identity"],
            "facts": [{"name": "hidden", "value": "1234"}],
            "disclosures": [],
        }
    }
    with pytest.raises(Refused, match="not disclosed"):
        verification_digits(call, "hidden", "", "", recipient_input=True)
