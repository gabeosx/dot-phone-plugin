"""Input filter placement before native model/VAD, using an offline stand-in."""

import audioop
import base64

import pytest

pytest.importorskip("pipecat")
from pipecat.audio.filters.base_audio_filter import BaseAudioFilter

from tests.test_general_vad_threshold import input_settings
from tests.test_realtime_interruption_ordering import audio_pipeline as audio_pipeline
from tests.test_realtime_interruption_ordering import (
    no_external_connections as no_external_connections,
)
from tests.test_realtime_playout import eventually


@pytest.fixture(autouse=True)
def filtered(settings, monkeypatch):
    instances = []

    class FixtureFilter(BaseAudioFilter):
        def __init__(self):
            self.inputs, self.stopped = [], False
            instances.append(self)

        async def start(self, sample_rate):
            assert sample_rate == 8000

        async def stop(self):
            self.stopped = True

        async def process_frame(self, frame):
            pass

        async def filter(self, audio):
            self.inputs.append(audio)
            return b"\x00\x10" * (len(audio) // 2)

    settings.pipecat_input_filter = "deepfilter"
    monkeypatch.setattr("dots_phone.deepfilter_filter.deepfilter_factory", lambda _: FixtureFilter)
    yield instances
    assert instances and all(instance.stopped for instance in instances)


async def test_filtered_audio_reaches_native_model_with_unchanged_vad(audio_pipeline, filtered):
    for _ in range(9):
        audio_pipeline.feed_input()
    await eventually(
        lambda: any(e["type"] == "input_audio_buffer.append" for e in audio_pipeline.model.sent)
    )
    assert len(filtered) == 1
    assert all(a == b"\0" * 320 for a in filtered[0].inputs)
    pcm = b"".join(
        base64.b64decode(e["audio"])
        for e in audio_pipeline.model.sent
        if e["type"] == "input_audio_buffer.append"
    )
    assert audioop.rms(pcm, 2) > 3000
    detector = input_settings(audio_pipeline)["turn_detection"]
    assert detector["create_response"] and detector["interrupt_response"]
    assert detector["threshold"] == 0.5 and audio_pipeline.carrier.clears == 0
