import asyncio
import importlib.util
from pathlib import Path

import pytest


def load_module(name):
    root = Path(__file__).resolve().parent.parent / "experiments/pipecat"
    if not (root / (name + ".py")).exists():
        root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(name + "_fixture", root / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_read_only_prefetch_waits_for_quiet_and_does_not_duplicate_delivery():
    module = load_module("early_lookup")
    reads, deliveries = [], []

    async def lookup():
        reads.append("museum")
        return {"synthetic": True, "place": "museum", "closes": "fixture time"}

    async def publish(result, revision, current):
        assert current(revision)
        deliveries.append(result)
        return "fixture-event"

    state = module.EarlyMuseumLookup(lookup, publish, lambda *args, **kwargs: None)
    state.begin_turn()
    state.observe("Tell a museum story")
    assert state.lookup_task is None
    state.begin_turn()
    state.observe("What is the museum's closing")
    assert state.lookup_task is None
    state.observe(" time?")
    await state.lookup_task
    assert len(reads) == 1 and deliveries == []
    state.quiet()
    await state.publish_task
    state.quiet()
    assert len(deliveries) == 1
    await state.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("correction", [False, True])
async def test_prefetch_discards_obsolete_requests_before_publishing(correction):
    module = load_module("early_lookup")
    release = asyncio.Event()
    deliveries, journal = [], []

    async def lookup():
        await release.wait()
        return {"closes": "fixture time"}

    async def publish(*args):
        deliveries.append(args)
        return "fixture-event"

    state = module.EarlyMuseumLookup(lookup, publish, lambda kind, **fields: journal.append(kind))
    state.begin_turn()
    state.observe("Museum closing time")
    task = state.lookup_task
    if correction:
        state.observe(" — actually the library instead")
    else:
        state.begin_turn()
    state.quiet()
    release.set()
    await task
    assert state.result is None and deliveries == []
    assert "prefetch_discarded" in journal
    await state.close()


def test_realtime_usage_includes_cancelled_response_and_rejects_missing_usage():
    pytest.importorskip("pipecat")
    from types import SimpleNamespace

    module = load_module("realtime_probe")
    service = module.MeasuredRealtime(
        evidence=SimpleNamespace(mark=lambda *a, **k: None, errors=[]), api_key="offline"
    )
    for sid in ["greeting", "interrupted"]:
        service.observe({"type": "response.created", "response": {"id": sid}})
        service.observe(
            {
                "type": "response.done",
                "response": {
                    "id": sid,
                    "status": "cancelled" if sid == "interrupted" else "completed",
                    "usage": {"total_tokens": 5},
                },
            }
        )
    assert service.usage_report()["usage_confirmed"]
    service.observe({"type": "response.created", "response": {"id": "unknown"}})
    service.observe({"type": "response.done", "response": {"id": "unknown", "status": "cancelled"}})
    assert not service.usage_report()["usage_confirmed"]


@pytest.mark.asyncio
async def test_realtime_returns_tool_result_without_waiting_for_paced_context_and_deduplicates():
    pytest.importorskip("pipecat")
    from types import SimpleNamespace

    module = load_module("realtime_probe")
    service = module.MeasuredRealtime(
        evidence=SimpleNamespace(close_sent=False, mark=lambda *a, **k: None, errors=[]),
        api_key="offline",
    )
    sequence = []

    async def result_callback(result):
        assert "fixture-call" in service._completed_tool_calls
        sequence.append("queued-context")

    async def wire(call_id, output):
        sequence.append("provider-output")

    async def continuation():
        sequence.append("continue")

    service._send_tool_result, service._create_response = wire, continuation
    params = SimpleNamespace(tool_call_id="fixture-call", result_callback=result_callback)
    await service.deliver_tool_result(params, {"closes": "fixture time"})
    assert sequence == ["queued-context", "provider-output", "continue"]
    assert "fixture-call" in service._completed_tool_calls
    service.tool_names = {"fixture-call": "lookup_hours"}
    assert not service.usage_report()["tool_outputs_transmitted"]
    service.sent(
        {
            "type": "conversation.item.create",
            "item": {"type": "function_call_output", "call_id": "fixture-call"},
        }
    )
    assert service.usage_report()["tool_outputs_transmitted"]
