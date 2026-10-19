"""Offline checks of opening instructions on the real SDK wire, not model behavior."""

from types import SimpleNamespace

import pytest

pytest.importorskip("pipecat")

from tests.test_realtime_interruption_ordering import audio_pipeline as audio_pipeline
from tests.test_realtime_interruption_ordering import (
    no_external_connections as no_external_connections,
)
from tests.test_realtime_playout import eventually

OLD_STARTUP = (
    "Introduce yourself as Dots Phone, an AI assistant, and briefly describe the approved "
    "purpose in one sentence of at most 15 words."
)


@pytest.fixture
def service(service, request, monkeypatch, tmp_path):
    route = getattr(request, "param", "general")
    if route == "hotel":
        from dots_phone import local_context_worker, prepared_evidence

        service.local_context_worker = SimpleNamespace(
            schema=3, directory=tmp_path, registered=lambda call: True, scope=lambda call: {}
        )
        monkeypatch.setattr(local_context_worker, "hotel_instructions", lambda call: "Hotel route")
        monkeypatch.setattr(prepared_evidence, "prepared_instructions", lambda *args: "")
    elif route == "gmail":
        from dots_phone import local_gmail_worker

        service.local_gmail_worker = SimpleNamespace(
            registered=lambda call: True, scope=lambda call: {"narrow_tool": True}
        )
        monkeypatch.setattr(local_gmail_worker, "marker_instructions", lambda call: "Gmail route")
    elif route == "mail":
        from dots_phone import local_mail_worker

        service.local_mail_worker = SimpleNamespace(
            registered=lambda call: True, scope=lambda call: {}
        )
        monkeypatch.setattr(local_mail_worker, "mail_instructions", lambda call: "Mail route")
    elif route == "keypad":
        from dots_phone import keypad_tools, pipecat_voice

        monkeypatch.setattr(
            pipecat_voice.PipecatTelephony, "account_keypad_enabled", lambda *a: True
        )
        monkeypatch.setattr(
            keypad_tools, "task_keypad_tools", lambda telephony, llm, call, tools, **kw: (tools, "")
        )
    return service


def session_wire(pipeline):
    updates = [event for event in pipeline.model.sent if event["type"] == "session.update"]
    assert updates
    return updates[-1]["session"]


def user_wire(pipeline):
    return [
        part["text"]
        for event in pipeline.model.sent
        if event["type"] == "conversation.item.create" and event["item"].get("role") == "user"
        for part in event["item"]["content"]
        if part.get("type") == "input_text"
    ]


async def test_general_opening_is_purpose_first_in_system_and_initial_context(audio_pipeline):
    pipeline = audio_pipeline
    await eventually(lambda: user_wire(pipeline))
    prompt = session_wire(pipeline)["instructions"]
    startup = user_wire(pipeline)
    assert len(startup) == 1
    assert "When first speaking to a live person, ask the approved task question" in prompt
    for text in (prompt[prompt.index("When first speaking to a live person") :], startup[0]):
        assert text.index("request first") < text.index("identify yourself as an AI assistant")
        assert "in the same opening turn" in text
    assert OLD_STARTUP not in startup
    assert "without restarting the introduction" in prompt
    assert "Silence alone is not a reason to repeat" in prompt
    assert "Do not repeat after an answer, refusal, hold request" in prompt
    assert "Stay silent through automated greetings and menus" in startup[0]
    # This is only an instruction change. The native VAD and output barrier stay
    # active; a server speech event must still reach the carrier clear path.
    assert session_wire(pipeline)["audio"]["input"]["turn_detection"] == {
        "type": "server_vad",
        "threshold": 0.5,
        "prefix_padding_ms": 300,
        "silence_duration_ms": 300,
        "create_response": True,
        "interrupt_response": True,
        "idle_timeout_ms": None,
    }
    assert pipeline.llm.interruption_barrier is not None
    pipeline.model.speech(started=True, turn=1)
    await eventually(lambda: pipeline.carrier.clears == 1)
    assert not [event for event in pipeline.model.sent if event["type"] == "response.cancel"]


@pytest.mark.parametrize("service", ["hotel", "gmail", "mail"], indirect=True)
async def test_registered_routes_keep_their_existing_opening(service, audio_pipeline):
    await eventually(lambda: user_wire(audio_pipeline))
    assert (
        "When first speaking to a live person" not in session_wire(audio_pipeline)["instructions"]
    )
    assert user_wire(audio_pipeline) == [OLD_STARTUP]


@pytest.mark.parametrize("service", ["keypad"], indirect=True)
async def test_account_keypad_route_remains_listen_first(service, audio_pipeline):
    assert (
        "When first speaking to a live person" not in session_wire(audio_pipeline)["instructions"]
    )
    assert user_wire(audio_pipeline) == []
    assert not [event for event in audio_pipeline.model.sent if event["type"] == "response.create"]
    assert audio_pipeline.llm._context.messages == [
        {
            "role": "user",
            "content": "Listen for the recipient's menu or greeting before responding.",
        }
    ]
