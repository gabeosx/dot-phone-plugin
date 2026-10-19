"""The wire phase must gate speech before the pinned SDK drops that metadata."""

import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("pipecat")

from pipecat.frames.frames import TTSAudioRawFrame, TTSTextFrame
from pipecat.services.openai.realtime import events

from dots_phone.pipecat_realtime import SupervisedRealtimeService


def voice(service, brief):
    cid = service.prepare("owner", brief)["call_id"]
    llm = SupervisedRealtimeService(supervisor=service, call_id=cid, api_key="offline-fixture")
    llm.push_frame = AsyncMock()
    llm.stop_ttfb_metrics = AsyncMock()
    return cid, llm


def item(phase, item_id="speech", event_type="response.output_item.added"):
    return {
        "type": event_type,
        "event_id": "fixture",
        "response_id": "response",
        "output_index": 0,
        "item": {"id": item_id, "type": "message", "role": "assistant", "phase": phase},
    }


def audio(item_id="speech"):
    return SimpleNamespace(
        item_id=item_id,
        response_id="response",
        content_index=0,
        output_index=0,
        delta=base64.b64encode(b"\x01\x00" * 240).decode(),
    )


@pytest.mark.parametrize("event_type", ["response.output_item.added", "conversation.item.added"])
async def test_commentary_never_becomes_audio_text_or_observed_speech(service, brief, event_type):
    cid, llm = voice(service, brief)
    raw = item("commentary", event_type=event_type)
    if event_type == "response.output_item.added":
        # The adapter must read phase before the pinned SDK silently discards it.
        assert not hasattr(events.parse_server_event(json.dumps(raw)).item, "phase")
    await llm.observe(raw)
    await llm._handle_evt_audio_delta(audio())
    await llm._handle_evt_audio_transcript_delta(
        SimpleNamespace(item_id="speech", delta="Let me finalize this properly.")
    )
    await llm.observe(
        {
            "type": "response.output_audio_transcript.done",
            "item_id": "speech",
            "transcript": "Let me finalize this properly.",
        }
    )
    llm.push_frame.assert_not_awaited()
    assert llm._current_audio_response is None and not llm.dialog
    llm.save_usage()
    usage = service.call("owner", cid)["usage"]["realtime_speech_delivery"]
    assert usage["model_output_audio_events"] == 1
    assert usage["model_output_audio_bytes"] == 480
    assert usage["forwarded_audio_bytes"] == 0
    assert usage["suppressed_audio_bytes"] == 480
    assert usage["suppressed_transcript_events"] == 2
    assert "finalize" not in json.dumps(usage)


@pytest.mark.parametrize("phase", ["final_answer", None, "future_phase"])
async def test_actual_answers_and_unlabelled_older_voices_still_stream(service, brief, phase):
    cid, llm = voice(service, brief)
    await llm.observe(item(phase))
    await llm._handle_evt_audio_delta(audio())
    await llm._handle_evt_audio_transcript_delta(
        SimpleNamespace(item_id="speech", delta="April eighteenth.")
    )
    await llm.observe(
        {
            "type": "response.output_audio_transcript.done",
            "item_id": "speech",
            "transcript": "April eighteenth.",
        }
    )
    frames = [call.args[0] for call in llm.push_frame.await_args_list]
    assert any(isinstance(frame, TTSAudioRawFrame) and len(frame.audio) == 480 for frame in frames)
    assert any(isinstance(frame, TTSTextFrame) for frame in frames)
    assert llm.dialog == [{"role": "assistant", "text": "April eighteenth."}]
    llm.save_usage()
    usage = service.call("owner", cid)["usage"]
    delivery = usage["realtime_speech_delivery"]
    assert delivery["model_output_audio_events"] == 1
    assert delivery["model_output_audio_bytes"] == delivery["forwarded_audio_bytes"] == 480
    assert delivery["audio_format"] == "pcm_s16le/24000/mono"
    # Receiving model output must never increment the input-direction counter.
    assert usage["realtime_media_progress"]["model_input_audio_packets_sent"] == 0


async def test_silent_commentary_keeps_tools_and_later_answer_in_same_response(service, brief):
    _, llm = voice(service, brief)
    await llm.observe(item("commentary"))
    await llm._handle_evt_audio_delta(audio())
    llm.run_function_calls = AsyncMock()
    await llm._handle_evt_conversation_item_added(
        SimpleNamespace(
            item=events.ConversationItem(
                type="function_call", call_id="lookup", name="read_call_source", arguments="{}"
            )
        )
    )
    await llm._handle_evt_function_call_arguments_done(
        SimpleNamespace(call_id="lookup", arguments="{}")
    )
    assert llm.run_function_calls.await_args.args[0][0].function_name == "read_call_source"
    await llm.observe(item("final_answer", "answer"))
    await llm._handle_evt_audio_delta(audio("answer"))
    assert (
        sum(isinstance(call.args[0], TTSAudioRawFrame) for call in llm.push_frame.await_args_list)
        == 1
    )


async def test_late_commentary_metadata_is_reported_instead_of_claiming_it_was_muted(
    service, brief
):
    _, llm = voice(service, brief)
    await llm._handle_evt_audio_delta(audio())
    await llm.observe(item("commentary", event_type="response.output_item.done"))
    assert llm.speech_delivery["commentary_audio_bytes_before_phase"] == 480
    await llm._handle_evt_audio_delta(audio())
    assert llm.speech_delivery["suppressed_audio_bytes"] == 480
