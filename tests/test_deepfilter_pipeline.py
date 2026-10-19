"""Real native streaming filter through simulated carrier/Realtime; no network.

Explicit external weights/library are required; ordinary CI never downloads them.
"""

import asyncio
import audioop
import base64
import math
import os
from array import array
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import SecretStr

pytest.importorskip("pipecat")

from pipecat.services.openai.realtime import llm as realtime_sdk

from dots_phone.pipecat_voice import PipecatTelephony
from tests.test_general_call_reasoner import prepare_general, start
from tests.test_realtime_interruption_ordering import (
    no_external_connections as no_external_connections,
)
from tests.test_realtime_playout import MemoryCarrier, MemoryRealtime


async def until(predicate):
    async with asyncio.timeout(12):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("mode", ["deepfilter", "off"])
async def test_real_filter_survives_keypad_rebind_and_closes(
    service, settings, brief, tmp_path, monkeypatch, mode
):
    model_path = os.environ.get("DOTS_PHONE_TEST_DEEPFILTER_MODEL")
    if mode == "deepfilter" and not model_path:
        pytest.skip("Explicit external DeepFilterNet3 model not supplied")
    sample = array(
        "h", (int(4000 * math.sin(i * 2 * math.pi * 311 / 8000)) for i in range(24000))
    ).tobytes()
    settings.pipecat_engine = "realtime"
    settings.pipecat_account_dtmf = True
    settings.pipecat_input_filter = mode
    settings.deepfilter_model_path = Path(model_path or "/absent-model-not-needed-when-off")
    settings.deepfilter_library_path = Path(
        os.environ.get("DOTS_PHONE_TEST_DEEPFILTER_LIBRARY", "/opt/deepfilter/lib/libdf.so")
    )
    settings.twilio_account = SecretStr("AC" + "2" * 32)
    settings.openai_key = SecretStr("offline-fixture")
    prepared = prepare_general(
        service,
        brief,
        tmp_path,
        actions=["ask_question", "record_call_result", "navigate_phone_menu"],
    )
    service.local_context_worker.before_dial = AsyncMock()
    start(service, prepared)
    await service.tick()
    call = service.call("owner", prepared["call_id"])
    model = MemoryRealtime()
    connect = AsyncMock(return_value=model)
    monkeypatch.setattr(realtime_sdk, "websocket_connect", connect)
    client = MagicMock()
    client.calls.create.side_effect = AssertionError("Must not dial a real call")
    client.calls.return_value.update.return_value = SimpleNamespace(sid=call["provider_sid"])
    phone = PipecatTelephony(settings, service, client)
    factory = phone.input_filter_factory
    filters = []

    def observed_factory():
        value = factory()
        filters.append(value)
        return value

    phone.input_filter_factory = observed_factory if factory else None
    params = {
        "accountSid": settings.twilio_account.get_secret_value(),
        "callSid": call["provider_sid"],
        "streamSid": "MZ" + "3" * 32,
        "customParameters": {"dots_call_id": call["call_id"]},
    }
    one = MemoryCarrier(params)
    tasks = [asyncio.create_task(phone.media(one))]
    two = None
    try:
        await until(lambda: call["provider_sid"] in phone.sessions)
        session = phone.sessions[call["provider_sid"]]
        await asyncio.wait_for(session.llm.ready.wait(), 12)

        def feed(carrier):
            for offset in range(0, len(sample), 320):
                pcm = audioop.lin2ulaw(sample[offset : offset + 320], 2)
                carrier.feed(
                    {"event": "media", "media": {"payload": base64.b64encode(pcm).decode()}}
                )

        def received():
            return sum(
                len(base64.b64decode(e["audio"]))
                for e in model.sent
                if e["type"] == "input_audio_buffer.append"
            )

        feed(one)
        await until(lambda: received() > 24000)
        if mode == "off":
            from pipecat.audio.utils import create_stream_resampler

            decoded = audioop.ulaw2lin(audioop.lin2ulaw(sample, 2), 2)
            resampler = create_stream_resampler()
            expected = b"".join(
                [
                    await resampler.resample(decoded[i : i + 320], 8000, 24000)
                    for i in range(0, len(decoded), 320)
                ]
            )
            actual = b"".join(
                base64.b64decode(e["audio"])
                for e in model.sent
                if e["type"] == "input_audio_buffer.append"
            )
            # Independent native int16 resampling paths can differ by two PCM
            # quantization units. Require the same sample-aligned waveform,
            # allowing only that numerical variation (2/32768 full scale).
            actual_samples = array("h", actual)
            expected_samples = array("h", expected[: len(actual)])
            assert len(actual_samples) == len(expected_samples)
            assert (
                max(
                    (abs(a - b) for a, b in zip(actual_samples, expected_samples, strict=True)),
                    default=0,
                )
                <= 2
            )
        assert len(filters) == (1 if mode == "deepfilter" else 0)
        state = filters[0].worker if filters else None
        assert not filters or (state is not None and not filters[0].bypassed)
        session.llm.dialog.append({"role": "user", "text": "For fares, press one."})
        control = asyncio.create_task(phone.press_phone_key(call["call_id"], "1"))
        tasks.append(control)
        await until(lambda: session.carrier.pending_nonce is not None)
        two = MemoryCarrier(
            {
                **params,
                "streamSid": "MZ" + "4" * 32,
                "customParameters": {
                    **params["customParameters"],
                    "dots_reattach_nonce": session.carrier.pending_nonce,
                },
            }
        )
        tasks.append(asyncio.create_task(phone.media(two)))
        result = await asyncio.wait_for(control, 12)
        assert result["carrier_control_accepted"] and result["audio_reconnected"]
        assert len(filters) == (1 if mode == "deepfilter" else 0)
        assert not filters or filters[0].worker is state
        before = received()
        feed(two)
        await until(lambda: received() > before + 24000)
        assert connect.await_count == 1  # Same model and filter through keypad stream replacement.
        assert session.carrier.stream_count == 2
        if filters:
            # A native crash bypasses the filter, never the live call/model.
            state.process.kill()
            await state.process.wait()
            before = received()
            feed(two)
            await until(lambda: filters[0].bypassed and received() > before + 24000)
            assert filters[0].worker is None and connect.await_count == 1
            fresh = service.call("owner", call["call_id"])
            assert fresh["usage"]["input_audio_filter"]["bypassed"] is True
        client.calls.create.assert_not_called()
    finally:
        if two:
            await two.close()
        await one.close()
        await asyncio.wait_for(asyncio.gather(*tasks), 12)
    assert not filters or (filters[0].worker is None and not filters[0].started)
    assert not phone.sessions
