import pytest

pytest.importorskip("pipecat")

from dots_phone.pipecat_realtime import SupervisedRealtimeService
from tests.test_pipecat_realtime import started


@pytest.mark.parametrize("retain", [False, True])
async def test_recipient_confirmation_order_tracks_audio_not_asr_delivery(service, brief, retain):
    service.settings.retain_transcript = retain
    call_id, _ = await started(service, brief)
    llm = SupervisedRealtimeService(supervisor=service, call_id=call_id, api_key="offline-fixture")
    for item in ("old", "new"):
        await llm.observe({"type": "input_audio_buffer.speech_started", "item_id": item})
        await llm.observe({"type": "input_audio_buffer.speech_stopped", "item_id": item})
    # The old ASR can arrive after the clarification audio; its turn stays old.
    for item in ("new", "old", "new"):
        await llm.observe(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": item,
                "transcript": "The same words.",
            }
        )
    assert service.call("owner", call_id)["recipient_audio_turn"] == 2
    entries = service.transcripts[call_id]
    assert [(e["recipient_item_id"], e["recipient_turn"]) for e in entries] == [
        ("new", 2),
        ("old", 1),
    ]
    archive = service.call("owner", call_id).get("transcript", [])
    assert bool(archive) is retain
    if retain:
        assert [e["recipient_turn"] for e in archive] == [2, 1]


async def test_transcription_without_observed_audio_cannot_claim_new_confirmation(service, brief):
    call_id, _ = await started(service, brief)
    llm = SupervisedRealtimeService(supervisor=service, call_id=call_id, api_key="offline-fixture")
    await llm.observe(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "unbound",
            "transcript": "Yes.",
        }
    )
    assert "recipient_turn" not in service.transcripts[call_id][0]
    assert not service.call("owner", call_id).get("recipient_audio_turn")


async def test_new_voice_adapter_continues_persisted_recipient_order(service, brief):
    call_id, _ = await started(service, brief)
    for item in ("one", "two"):
        llm = SupervisedRealtimeService(
            supervisor=service, call_id=call_id, api_key="offline-fixture"
        )
        await llm.observe({"type": "input_audio_buffer.speech_started", "item_id": item})
        await llm.observe({"type": "input_audio_buffer.speech_started", "item_id": item})
        await llm.observe(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": item,
                "transcript": "Yes.",
            }
        )
    assert service.transcripts[call_id][0]["recipient_turn"] == 2
