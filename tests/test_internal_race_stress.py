"""Frozen async schedules against production controls; no models or carrier network.

The oracle checks structural ownership and external side effects, not whether
specific words imply a menu choice. Tool arguments stand in for model decisions.
"""

import asyncio
import json
import os
import random
import socket
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("pipecat")

from dots_phone.models import Steer
from dots_phone.service import Refused
from tests.test_control_provenance import (
    control_voice,
    function_event,
    invoke,
    recipient,
    response,
    tool_params,
)

SEEDS = tuple(range(4100, 4132))
PROFILES = ("origin", "authority", "submitted", "lookup")


async def yield_steps(rng):
    for _ in range(rng.randrange(1, 4)):
        await asyncio.sleep(0)


async def asr(llm, item, text, index=0):
    await llm.observe(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": item,
            "content_index": index,
            "transcript": text,
        }
    )


async def change_scope(service, cid, llm, change, trace):
    trace.append({"event": "scope_change", "change": change})
    if change == "stop":
        assert llm.request_stop("The recipient wants this test conversation ended.")
    elif change == "steer":
        await service.steer(
            "owner", Steer(call_id=cid, revision=1, instruction="Continue the approved task.")
        )
    elif change == "revoke":
        call = service.call("owner", cid)
        grant = service.store.get("approval", call["started_authorization"], "owner")
        grant["consumed"] = False
        service.store.put("approval", grant["id"], "owner", grant)
    elif change == "end":
        call = service.call("owner", cid)
        call["state"] = "completed"
        service.save(call)
    elif change == "budget":
        service.settings.max_seconds = 1
    elif change == "new_input":
        await llm.observe({"type": "input_audio_buffer.speech_started", "item_id": "new"})


async def blocked_action(llm, session, tools, operation, gate, rng, trace):
    """Return a suspended real handler plus a deterministic release function."""
    entered, release = asyncio.Event(), asyncio.Event()
    if gate == "lock":
        await session.menu_lock.acquire()
    elif gate == "clear":

        async def wait_clear(*_):
            trace.append({"event": "clear_entered"})
            entered.set()
            await release.wait()

        llm.clear_playback.side_effect = wait_clear
    await response(llm, "old-response")
    if gate != "late_tool":
        await function_event(llm, "old-response", "old-action")
        task = asyncio.create_task(invoke(tools, operation, tool_params("old-action")))
        if gate == "clear":
            await asyncio.wait_for(entered.wait(), 1)
        else:
            await yield_steps(rng)
            assert not task.done()
    else:
        task = None

    async def resume():
        nonlocal task
        trace.append({"event": "handler_release", "gate": gate})
        if gate == "late_tool":
            kinds = (
                "response.output_item.added",
                "response.output_item.done",
                "response.function_call_arguments.done",
            )
            await function_event(llm, "old-response", "old-action", kind=rng.choice(kinds))
            task = asyncio.create_task(invoke(tools, operation, tool_params("old-action")))
        elif gate == "lock":
            session.menu_lock.release()
        else:
            release.set()
        await asyncio.wait_for(task, 1)
        llm.clear_playback.side_effect = None

    return resume


async def fresh_action(llm, tools, operation, trace, *, response_id="fresh-response"):
    await response(llm, response_id)
    await function_event(llm, response_id, response_id + "-action")
    await invoke(tools, operation, tool_params(response_id + "-action"), corrected=True)
    result = llm.return_local_tool_result.call_args.args[1]
    trace.append({"event": "fresh_result", "result": result})
    assert result["carrier_control_accepted"] is True


async def run_origin(service, brief, seed, rng, trace):
    cid, llm, session, client, _, tools = await control_voice(service, brief)
    operation = rng.choice(("menu", "verification"))
    gate = rng.choice(("lock", "clear", "late_tool"))
    variant = seed % 4
    trace.append({"profile": "origin", "operation": operation, "gate": gate, "variant": variant})
    resume = await blocked_action(llm, session, tools, operation, gate, rng, trace)
    if variant == 0:
        # New audio makes the old command stale before any transcript exists.
        await llm.observe({"type": "input_audio_buffer.speech_started", "item_id": "new"})
        await yield_steps(rng)
        await asr(llm, "one", "Delayed remainder from the previous recording.", 1)
    elif variant == 1:
        await asr(llm, "one", "A late fragment with different wording.", 1)
    elif variant == 2:
        # Unknown ASR item is audit text, not a fabricated native speech turn.
        await asr(llm, "unmapped", "Incomplete and unrelated caption.")
    else:
        await asr(llm, "one", "", 1)
    await yield_steps(rng)
    await resume()
    result = llm.return_local_tool_result.call_args.args[1]
    trace.append({"event": "old_result", "result": result})
    expected = variant != 0
    assert result["carrier_control_accepted"] is expected
    assert client.calls.return_value.update.call_count == int(expected)
    assert len(session.used_menus) == int(expected)
    if expected:
        command = next(iter(service.call("owner", cid)["private_keypad_commands"].values()))
        assert command["recipient_audio_turn"] == 1
        assert command["originating_response_id"] == "old-response"
    else:
        assert result["input_status"] == "not_submitted"
        assert not service.call("owner", cid).get("private_keypad_commands")
        await llm.observe({"type": "input_audio_buffer.speech_stopped", "item_id": "new"})
        await fresh_action(llm, tools, operation, trace)
        assert client.calls.return_value.update.call_count == 1
        command = next(iter(service.call("owner", cid)["private_keypad_commands"].values()))
        assert command["recipient_audio_turn"] == 2


async def run_authority(service, brief, seed, rng, trace):
    cid, llm, session, client, _, tools = await control_voice(service, brief)
    operation = rng.choice(("menu", "verification"))
    gate = rng.choice(("lock", "clear", "late_tool"))
    change = ("stop", "steer", "revoke", "end", "budget", "new_input", "none", "none")[seed % 8]
    trace.append({"profile": "authority", "operation": operation, "gate": gate, "change": change})
    resume = await blocked_action(llm, session, tools, operation, gate, rng, trace)
    await change_scope(service, cid, llm, change, trace)
    await yield_steps(rng)
    await resume()
    result = llm.return_local_tool_result.call_args.args[1]
    trace.append({"event": "old_result", "result": result})
    expected = change == "none"
    assert result["carrier_control_accepted"] is expected
    assert client.calls.return_value.update.call_count == int(expected)
    assert len(session.used_menus) == int(expected)
    if change in {"steer", "new_input"}:
        await fresh_action(llm, tools, operation, trace)
        assert client.calls.return_value.update.call_count == 1
    if llm.stop_task:
        await asyncio.wait_for(llm.stop_task, 1)


async def run_submitted(service, brief, seed, rng, trace):
    cid, llm, session, client, _, tools = await control_voice(service, brief)
    operation = rng.choice(("menu", "verification"))
    variant = seed % 4
    entered, release = asyncio.Event(), asyncio.Event()

    async def reconnect(**_):
        trace.append({"event": "carrier_accepted_reconnect_wait"})
        entered.set()
        await release.wait()
        if variant == 0:
            raise TimeoutError("offline ambiguous recipient delivery")

    session.carrier.wait_change.side_effect = reconnect
    await response(llm, "submitted-response")
    await function_event(llm, "submitted-response", "first")
    await function_event(llm, "submitted-response", "queued-duplicate")
    first = asyncio.create_task(invoke(tools, operation, tool_params("first")))
    await asyncio.wait_for(entered.wait(), 1)
    second = asyncio.create_task(invoke(tools, operation, tool_params("queued-duplicate")))
    await yield_steps(rng)
    assert not second.done()
    assert client.calls.return_value.update.call_count == 1
    if variant == 1:
        await change_scope(service, cid, llm, "stop", trace)
    elif variant == 3:
        await recipient(llm, "new", "Use the current selection from this new request.")
    release.set()
    first_result, second_result = await asyncio.wait_for(
        asyncio.gather(first, second, return_exceptions=True), 1
    )
    if variant == 0:
        assert isinstance(first_result, RuntimeError) and "no automatic retry" in str(first_result)
        session.carrier.close.assert_awaited_once()
    else:
        assert first_result is None
    assert second_result is None
    assert client.calls.return_value.update.call_count == 1
    result = llm.return_local_tool_result.call_args.args[1]
    assert result["carrier_control_accepted"] is False
    trace.append({"event": "duplicate_result", "result": result, "variant": variant})
    assert len(session.used_menus) == 1
    if variant == 3:
        session.carrier.wait_change.side_effect = None
        await fresh_action(llm, tools, operation, trace)
        assert client.calls.return_value.update.call_count == 2
    if llm.stop_task:
        await asyncio.wait_for(llm.stop_task, 1)


async def run_lookup(service, brief, seed, rng, trace):
    cid, llm, session, client, _, tools = await control_voice(service, brief)
    llm._websocket = SimpleNamespace(send=AsyncMock())
    llm._create_response = AsyncMock()
    llm._truncate_current_audio_response = AsyncMock()
    # Freeze real response/tool origin before an independent lookup starts.
    await response(llm, "lookup-origin")
    await function_event(llm, "lookup-origin", "old-action")
    params = tool_params("lookup-result")
    future = asyncio.get_running_loop().create_future()
    llm.decisions[params.tool_call_id] = (params, future)
    llm.source_calls.add(params.tool_call_id)
    pending = service.pending_decision(cid, params.tool_call_id, "Read the approved source.")
    before = service.call("owner", cid)
    variant = seed % 4
    # Delivery is itself asynchronous and can queue behind another tool output.
    await llm.delivery_lock.acquire()
    delivery = asyncio.create_task(
        llm.deliver_decision(
            before, params.tool_call_id, "Approved context; choose the current action."
        )
    )
    await yield_steps(rng)
    assert not delivery.done()
    trace.append({"profile": "lookup", "variant": variant, "event": "delivery_queued"})
    if variant == 0:
        await change_scope(service, cid, llm, "steer", trace)
    elif variant == 1:
        await llm.observe({"type": "input_audio_buffer.speech_started", "item_id": "new"})
        await asr(llm, "one", "Late text from the prior question.", 1)
    llm.delivery_lock.release()
    try:
        if variant == 0:
            with pytest.raises(Refused, match="stale"):
                await asyncio.wait_for(delivery, 1)
            llm._websocket.send.assert_not_awaited()
        else:
            await asyncio.wait_for(delivery, 1)
            assert future.result() is True
            llm._websocket.send.assert_awaited_once()
            assert params.result_callback.call_args.kwargs["properties"].run_llm is False
            # A source result is data. Completion alone must never execute its
            # old action or create speech while the old response is still live.
            client.calls.return_value.update.assert_not_called()
            llm._create_response.assert_not_awaited()
            if variant == 2:
                await change_scope(service, cid, llm, "steer", trace)
            elif variant == 3:
                await change_scope(service, cid, llm, "stop", trace)
            await llm.observe(
                {
                    "type": "conversation.item.added",
                    "item": {"type": "function_call_output", "call_id": params.tool_call_id},
                }
            )
        # Only explicit fresh reasoning is allowed to act on newer context.
        if variant in {0, 1, 2}:
            await invoke(tools, "menu", tool_params("old-action"))
            old = llm.return_local_tool_result.call_args.args[1]
            assert not old["carrier_control_accepted"]
            assert not session.used_menus
            client.calls.return_value.update.assert_not_called()
            await fresh_action(llm, tools, "menu", trace)
            assert client.calls.return_value.update.call_count == 1
        else:
            await invoke(tools, "menu", tool_params("old-action"))
            client.calls.return_value.update.assert_not_called()
        llm.caller_speaking = False
        llm.caller_stopped_at = time.monotonic() - 1
        llm.active_responses.clear()
        if llm.source_continuation_task:
            await asyncio.wait_for(llm.source_continuation_task, 1)
        llm._create_response.assert_not_awaited()
        if llm.stop_task:
            await asyncio.wait_for(llm.stop_task, 1)
        trace.append({"event": "lookup_never_autoexecuted", "request_id_present": bool(pending)})
    finally:
        if not delivery.done():
            delivery.cancel()
        await asyncio.gather(delivery, return_exceptions=True)
        if llm.source_continuation_task and not llm.source_continuation_task.done():
            llm.source_continuation_task.cancel()
            await asyncio.gather(llm.source_continuation_task, return_exceptions=True)


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("seed", SEEDS)
async def test_seeded_internal_schedules(service, brief, monkeypatch, seed, profile):
    trace = []

    def no_network(*args, **kwargs):
        raise AssertionError("This internal test must never open a network connection")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    outcome = "failed"
    try:
        await globals()["run_" + profile](service, brief, seed, random.Random(seed), trace)
        outcome = "passed"
    except BaseException as exc:
        trace.append({"event": "exception", "type": type(exc).__name__, "message": str(exc)})
        raise
    finally:
        directory = os.environ.get("INTERNAL_RACE_TRACE_DIRECTORY")
        if directory:
            path = Path(directory)
            path.mkdir(parents=True, exist_ok=True)
            (path / f"{profile}-{seed}.json").write_text(
                json.dumps(
                    {
                        "seed": seed,
                        "profile": profile,
                        "outcome": outcome,
                        "phone_calls": 0,
                        "model_api_requests": 0,
                        "trace": trace,
                    },
                    indent=2,
                )
                + "\n"
            )
