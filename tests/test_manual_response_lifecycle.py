"""Synthetic wire events through the real, offline manual media/aggregator path."""

import asyncio
import json

import pytest

pytest.importorskip("pipecat")

from pipecat.frames.frames import LLMFullResponseStartFrame, TranscriptionFrame

from tests.test_realtime_interruption_ordering import (
    no_external_connections as no_external_connections,
)
from tests.test_realtime_manual_integration import (
    feed,
    synthetic_input,
)
from tests.test_realtime_manual_integration import (
    manual_pipeline as manual_pipeline,
)
from tests.test_realtime_playout import eventually


async def request_response(p):
    assert await p.source.start_turn()
    await feed(p, synthetic_input(300))
    assert await p.source.end_turn()
    await asyncio.wait_for(p.llm.manual.wait_idle(), 2)


def response_events(p, name, text):
    # Build valid fixture protocol messages without sending them until the
    # intended race point. No audio or transcript inference is involved.
    assert p.model.incoming.empty()
    p.model.response(name, milliseconds=0)
    created, item, done = [json.loads(p.model.incoming.get_nowait()) for _ in range(3)]
    transcript = {
        "type": "response.output_audio_transcript.delta",
        "response_id": name,
        "item_id": name + "_item",
        "output_index": 0,
        "content_index": 0,
        "delta": text,
    }
    return created, item, transcript, done


async def current_transcript(p, text):
    user = p.llm._user_aggregator
    await user.queue_frame(TranscriptionFrame(text, "caller", "2026-10-18T00:00:00Z"))
    await eventually(lambda: text in str(user._aggregation))


async def assert_fresh_context(p, caller_text):
    assert await p.source.end_turn()
    await asyncio.wait_for(p.llm.manual.wait_idle(), 2)
    for event in response_events(p, "fresh", "Fresh assistant words."):
        p.model.emit(event)
    await eventually(lambda: not p.llm._user_aggregator._aggregation)
    await eventually(lambda: "Fresh assistant words." in str(p.llm._context.get_messages()))
    messages = p.llm._context.get_messages()
    assert sum(caller_text in str(message) for message in messages) == 1
    assert "Revoked assistant words." not in str(messages)


async def test_late_old_item_text_and_end_do_not_flush_current_caller(manual_pipeline):
    p = manual_pipeline
    await request_response(p)
    old = response_events(p, "old", "Revoked assistant words.")
    assert await p.source.start_turn()
    await feed(p, synthetic_input(300))
    await current_transcript(p, "Current caller is still speaking.")
    before = list(p.llm._context.get_messages())
    for event in old:
        p.model.emit(event)
    await eventually(lambda: "old" in p.llm.response_usage)
    await asyncio.sleep(0.05)
    assert p.llm._context.get_messages() == before
    assert "Current caller is still speaking." in str(p.llm._user_aggregator._aggregation)
    assert p.llm._user_aggregator._realtime_handoff_flush_task is None
    await assert_fresh_context(p, "Current caller is still speaking.")


async def test_queued_owned_start_is_rechecked_at_context_consumption(manual_pipeline):
    p = manual_pipeline
    await request_response(p)
    assistant = p.llm.output._next
    await assistant.pause_processing_system_frames()
    old = response_events(p, "old", "Revoked assistant words.")
    try:
        for event in old:
            p.model.emit(event)
        await eventually(lambda: "old" in p.llm.response_usage)
        # The owned start/end/text are already downstream of the transport;
        # the next priority interruption may overtake them in this input queue.
        await eventually(lambda: assistant._FrameProcessor__input_queue.qsize() >= 3)
        assert await p.source.start_turn()
        await feed(p, synthetic_input(300))
        await current_transcript(p, "A new caller turn remains open.")
        before = list(p.llm._context.get_messages())
    finally:
        await assistant.resume_processing_system_frames()
    await asyncio.sleep(0.05)
    assert p.llm._context.get_messages() == before
    assert "A new caller turn remains open." in str(p.llm._user_aggregator._aggregation)
    assert p.llm._user_aggregator._realtime_handoff_flush_task is None
    await assert_fresh_context(p, "A new caller turn remains open.")


async def test_response_created_announces_exactly_one_owned_start(manual_pipeline):
    p = manual_pipeline
    await request_response(p)
    seen = []

    async def record(processor, frame):
        if isinstance(frame, LLMFullResponseStartFrame):
            seen.append(frame.metadata.get("manual_generation"))

    p.llm.add_event_handler("on_before_push_frame", record)
    created, item, transcript, done = response_events(p, "fresh", "Fresh assistant words.")
    for event in (created, created, item, transcript, done):
        p.model.emit(event)
    await eventually(lambda: "fresh" in p.llm.response_usage)
    assert seen == [("fresh", "", 0)]
