"""Recipient termination is independent of verified task completion."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("pipecat")

from dots_phone.pipecat_realtime import SupervisedRealtimeService
from dots_phone.recipient_control import recipient_farewell_in_dialog
from tests.conftest import started
from tests.test_realtime_interruption_ordering import audio_pipeline as audio_pipeline
from tests.test_realtime_interruption_ordering import (
    no_external_connections as no_external_connections,
)


async def hear(llm, text, item_id="farewell"):
    await llm.observe({"type": "input_audio_buffer.speech_started", "item_id": item_id})
    await llm.observe({"type": "input_audio_buffer.speech_stopped", "item_id": item_id})
    await llm.observe(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": item_id,
            "transcript": text,
        }
    )


async def test_observed_goodbye_and_tool_intent_end_without_claiming_task_success(service, brief):
    call_id, _ = await started(service, brief)
    llm = SupervisedRealtimeService(supervisor=service, call_id=call_id, api_key="offline-fixture")
    service.end = AsyncMock()
    await hear(llm, "Yeah that's right, six forty-five at Harbor Cafe.", "confirmation")
    await hear(llm, "Talk to you later. Goodbye")
    assert not service.call("owner", call_id).get("conclusion")
    assert not llm.stop_requested  # The raw transcript alone cannot end the call.
    assert not llm.request_stop()
    assert llm.request_stop("Goodbye.")
    await llm.stop_task
    service.end.assert_awaited_once_with("owner", call_id, "recipient_stop")
    assert not service.call("owner", call_id).get("conclusion")
    assert llm.request_stop("Goodbye.")
    await asyncio.sleep(0)
    service.end.assert_awaited_once()


@pytest.mark.parametrize(
    "text,previous",
    [
        ("She said goodbye.", ""),
        ('The story ends with "Goodbye."', ""),
        ("Say goodbye if you are finished.", ""),
        ("Don't say goodbye.", ""),
        ("Goodbye. Or say main menu for other options.", ""),
        ("Stop the story and check the closing time.", ""),
        ("The television said goodbye.", ""),
        ('"Goodbye."', ""),
        ("Goodbye?", ""),
        ("Goodbye. Actually, wait, I have another question.", ""),
        ("Goodbye.", "If that is all you need."),
        ("Goodbye.", "When you are finished,"),
        ("Goodbye.", "The menu says."),
        ("Goodbye.", "That was someone on the television."),
        ("Goodbye.", "The character in the story says."),
    ],
)
async def test_mentioned_conditional_or_background_farewell_does_not_authorize_stop(
    service, brief, text, previous
):
    call_id, _ = await started(service, brief)
    llm = SupervisedRealtimeService(supervisor=service, call_id=call_id, api_key="offline-fixture")
    service.end = AsyncMock()
    if previous:
        await hear(llm, previous, "context")
    await hear(llm, text)
    assert not recipient_farewell_in_dialog(llm.dialog, quote="Goodbye.")
    assert not llm.request_stop("Goodbye.")
    await asyncio.sleep(0)
    service.end.assert_not_awaited()
    assert not llm.stop_requested


@pytest.mark.parametrize("new_turn_finished", [False, True])
async def test_old_farewell_cannot_stop_a_new_native_turn(service, brief, new_turn_finished):
    call_id, _ = await started(service, brief)
    llm = SupervisedRealtimeService(supervisor=service, call_id=call_id, api_key="offline-fixture")
    service.end = AsyncMock()
    await hear(llm, "Goodbye.")
    await llm.observe({"type": "input_audio_buffer.speech_started", "item_id": "new-turn"})
    if new_turn_finished:
        await llm.observe({"type": "input_audio_buffer.speech_stopped", "item_id": "new-turn"})
    assert not llm.request_stop("Goodbye.")
    service.end.assert_not_awaited()


async def test_late_farewell_transcript_cannot_replace_newer_audio_evidence(service, brief):
    call_id, _ = await started(service, brief)
    llm = SupervisedRealtimeService(supervisor=service, call_id=call_id, api_key="offline-fixture")
    service.end = AsyncMock()
    await llm.observe({"type": "input_audio_buffer.speech_started", "item_id": "old-turn"})
    await llm.observe({"type": "input_audio_buffer.speech_stopped", "item_id": "old-turn"})
    await hear(llm, "Wait, I have another question.", "new-turn")
    await llm.observe(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "old-turn",
            "transcript": "Goodbye.",
        }
    )
    assert not llm.request_stop("Goodbye.")
    service.end.assert_not_awaited()


async def test_fresh_turn_still_requires_the_observed_farewell_quote(service, brief):
    call_id, _ = await started(service, brief)
    llm = SupervisedRealtimeService(supervisor=service, call_id=call_id, api_key="offline-fixture")
    service.end = AsyncMock()
    await hear(llm, "Bye.")
    assert not llm.request_stop("Goodbye.")
    assert llm.request_stop("Bye.")
    await llm.stop_task
    service.end.assert_awaited_once_with("owner", call_id, "recipient_stop")


async def test_task_finished_path_still_requires_a_durable_conclusion(service, brief):
    call_id, _ = await started(service, brief)
    llm = SupervisedRealtimeService(supervisor=service, call_id=call_id, api_key="offline-fixture")
    service.end = AsyncMock()
    assert not llm.request_stop(after_result=True)
    service.end.assert_not_awaited()
    call = service.call("owner", call_id)
    call["conclusion"] = {"status": "completed", "confirmed_facts": [], "commitments": []}
    service.save(call)
    assert llm.request_stop(after_result=True)
    await llm.stop_task
    service.end.assert_awaited_once_with("owner", call_id, "task_finished")


@pytest.mark.parametrize(
    "dialog,quote",
    [
        ([{"role": "assistant", "text": "Goodbye."}], "Goodbye."),
        ([{"role": "user", "text": "Goodbye."}], "Goodbye."),  # No native provenance.
        ([{"role": "user", "text": "Bye."}], "Goodbye."),  # Not the observed quote.
    ],
)
async def test_unobserved_or_unprovenanced_model_farewell_is_rejected(
    service, brief, dialog, quote
):
    call_id, _ = await started(service, brief)
    llm = SupervisedRealtimeService(supervisor=service, call_id=call_id, api_key="offline-fixture")
    service.end = AsyncMock()
    llm.dialog = dialog
    assert not llm.request_stop(quote)
    service.end.assert_not_awaited()


@pytest.mark.parametrize("text", ["Goodbye.", "Thanks, goodbye.", "Talk to you later. Goodbye."])
def test_direct_closing_clause_can_match_its_exact_model_quote(text):
    assert recipient_farewell_in_dialog([{"role": "user", "text": text}], quote="Goodbye.")


async def test_general_hangup_tool_gives_neutral_rejection_then_accepts_observed_goodbye(
    audio_pipeline, service
):
    pipeline = audio_pipeline
    service.end = AsyncMock()
    tool = pipeline.llm._functions["hang_up"].handler
    params = SimpleNamespace(result_callback=AsyncMock())
    await tool.invoke({"recipient_request": "Goodbye."}, params)
    feedback = params.result_callback.call_args.args[0]
    assert feedback["ended"] is False
    assert "story" not in feedback["guidance"]
    assert "listen silently" in feedback["guidance"]
    assert "Do not repeat completed information" in feedback["guidance"]
    service.end.assert_not_awaited()

    await hear(pipeline.llm, "Talk to you later. Goodbye")
    params.result_callback.reset_mock()
    await tool.invoke({"recipient_request": "Goodbye."}, params)
    await pipeline.llm.stop_task
    service.end.assert_awaited_once_with("owner", pipeline.call_id, "recipient_stop")
    params.result_callback.assert_not_awaited()
    assert not service.call("owner", pipeline.call_id).get("conclusion")
