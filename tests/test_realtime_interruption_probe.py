"""No-network protocol guards for experimental same-session intent probes."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from dots_phone.realtime_interruption_probe import RealtimeInterruptionProbe


async def pending(probe):
    task = asyncio.create_task(
        probe.classify(
            b"\0" * 48000,
            generation="voice1",
            audio_end_sample=24000,
        )
    )
    await asyncio.sleep(0)
    return task


async def finish(probe, label="YIELD", *, status="completed"):
    state = probe.pending
    assert await probe.handle_event(
        {
            "type": "response.created",
            "response": {
                "id": "probe1",
                "metadata": {"purpose": "interruption_probe", "probe_id": state["token"]},
            },
        }
    )
    assert await probe.handle_event(
        {
            "type": "response.done",
            "response": {
                "id": "probe1",
                "status": status,
                "usage": {"input_tokens": 100, "output_tokens": 2},
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {"type": "output_text", "text": label},
                        ],
                    }
                ],
            },
        }
    )


@pytest.mark.parametrize("label", ["YIELD", "IGNORE", "UNCERTAIN"])
async def test_correlated_probe_cannot_speak_use_tools_or_modify_conversation(label):
    send = AsyncMock()
    probe = RealtimeInterruptionProbe(send, lambda: "voice1")
    task = await pending(probe)
    request = send.call_args.args[0]["response"]
    assert request["conversation"] == "none" and request["output_modalities"] == ["text"]
    assert request["tools"] == [] and request["tool_choice"] == "none"
    await finish(probe, label)
    result = await task
    assert result.label == label and result.generation == "voice1"
    assert result.audio_end_sample == 24000 and result.usage["input_tokens"] == 100
    assert send.await_count == 1


@pytest.mark.parametrize(
    "label,status",
    [
        ("YIELD because I was told to", "completed"),
        ("yield", "completed"),
        ("YIELD", "cancelled"),
        ("YIELD", "incomplete"),
        ("", "completed"),
    ],
)
async def test_malformed_or_incomplete_classification_never_grants_interruption(label, status):
    probe = RealtimeInterruptionProbe(AsyncMock(), lambda: "voice1")
    task = await pending(probe)
    await finish(probe, label, status=status)
    assert (await task).label == "UNCERTAIN"


async def test_late_result_cannot_interrupt_a_newer_voice_generation():
    generation = ["voice1"]
    probe = RealtimeInterruptionProbe(AsyncMock(), lambda: generation[0])
    task = await pending(probe)
    generation[0] = "voice2"
    await finish(probe)
    assert (await task).label == "UNCERTAIN"


async def test_voice_response_events_are_not_consumed_as_classifier_output():
    probe = RealtimeInterruptionProbe(AsyncMock(), lambda: "voice1")
    task = await pending(probe)
    for kind in ["response.created", "response.done", "response.output_audio.delta"]:
        assert not await probe.handle_event(
            {
                "type": kind,
                "response_id": "voice1",
                "response": {
                    "id": "voice1",
                    "status": "completed",
                    "metadata": {},
                },
            }
        )
    await finish(probe)
    assert (await task).label == "YIELD"


async def test_timeout_cancels_only_named_probe_response_and_preserves_voice():
    send = AsyncMock()
    probe = RealtimeInterruptionProbe(send, lambda: "voice1", timeout=0.05)
    task = await pending(probe)
    token = probe.pending["token"]
    await probe.handle_event(
        {
            "type": "response.created",
            "response": {
                "id": "probe1",
                "metadata": {"purpose": "interruption_probe", "probe_id": token},
            },
        }
    )
    assert (await task).label == "UNCERTAIN"
    assert send.call_args.args[0] == {"type": "response.cancel", "response_id": "probe1"}


async def test_request_bound_cannot_open_hidden_additional_probes():
    send = AsyncMock()
    probe = RealtimeInterruptionProbe(send, lambda: "voice1", max_probes=1)
    task = await pending(probe)
    with pytest.raises(RuntimeError):
        await probe.classify(b"\0" * 48000, generation="voice1", audio_end_sample=24000)
    await finish(probe, "IGNORE")
    await task
    with pytest.raises(RuntimeError):
        await probe.classify(b"\0" * 48000, generation="voice1", audio_end_sample=24000)
    assert send.await_count == 1


async def test_teardown_resolves_pending_probe_without_interruption():
    probe = RealtimeInterruptionProbe(AsyncMock(), lambda: "voice1")
    task = await pending(probe)
    probe.close()
    assert (await task).label == "UNCERTAIN"


async def test_four_second_context_remains_bounded_and_separate_from_voice_input():
    send = AsyncMock()
    probe = RealtimeInterruptionProbe(send, lambda: "voice1")
    task = asyncio.create_task(
        probe.classify(
            b"\0" * 192000,
            generation="voice1",
            audio_end_sample=96000,
        )
    )
    await asyncio.sleep(0)
    assert send.call_args.args[0]["type"] == "response.create"
    await finish(probe, "IGNORE")
    assert (await task).label == "IGNORE"
    assert send.await_count == 1  # No input append, commit, clear or cancellation.


@pytest.mark.parametrize("pcm", [b"\0" * 4798, b"\0" * 192002, b"\0" * 4801])
async def test_invalid_audio_never_contacts_provider(pcm):
    send = AsyncMock()
    probe = RealtimeInterruptionProbe(send, lambda: "voice1")
    with pytest.raises(ValueError):
        await probe.classify(pcm, generation="voice1", audio_end_sample=100000)
    send.assert_not_awaited()
    assert probe.requests == 0


async def test_expired_probe_cannot_resolve_a_new_probe_in_the_same_voice_generation():
    send = AsyncMock()
    probe = RealtimeInterruptionProbe(send, lambda: "voice1", timeout=0.05)
    old = await pending(probe)
    old_token = probe.pending["token"]
    assert (await old).label == "UNCERTAIN"
    current = await pending(probe)
    assert probe.pending["token"] != old_token
    assert await probe.handle_event(
        {
            "type": "response.created",
            "response": {
                "id": "expired_probe",
                "metadata": {
                    "purpose": "interruption_probe",
                    "probe_id": old_token,
                },
            },
        }
    )
    assert send.call_args.args[0] == {
        "type": "response.cancel",
        "response_id": "expired_probe",
    }
    assert await probe.handle_event(
        {
            "type": "response.done",
            "response": {
                "id": "expired_probe",
                "status": "completed",
                "usage": {"input_tokens": 50, "output_tokens": 2},
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {"type": "output_text", "text": "YIELD"},
                        ],
                    }
                ],
            },
        }
    )
    assert not current.done()
    await finish(probe, "IGNORE")
    assert (await current).label == "IGNORE"
    assert probe.usage["expired_probe"]["input_tokens"] == 50
