"""Keep image/document decoders outside the public telephone media boundary."""

import base64
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("pipecat")

from PIL import Image
from pipecat.frames.frames import InputAudioRawFrame
from pipecat.serializers.twilio import TwilioFrameSerializer


@pytest.mark.parametrize("event", ["image", "video", "document", "file"])
async def test_phone_serializer_never_decodes_uploaded_files(monkeypatch, event):
    def reject(*args, **kwargs):
        raise AssertionError("Telephone input must never invoke an image decoder")

    monkeypatch.setattr(Image, "open", reject)
    monkeypatch.setattr(Image, "frombytes", reject)
    serializer = TwilioFrameSerializer(
        stream_sid="MZfixture", params=TwilioFrameSerializer.InputParams(auto_hang_up=False)
    )
    await serializer.setup(SimpleNamespace(audio_in_sample_rate=8000))
    payload = base64.b64encode(b"II*\x00synthetic-tiff-header").decode()
    assert (
        await serializer.deserialize(json.dumps({"event": event, event: {"payload": payload}}))
        is None
    )
    audio = await serializer.deserialize(
        json.dumps({"event": "media", "media": {"payload": payload}})
    )
    assert isinstance(audio, InputAudioRawFrame)
    assert audio.sample_rate == 8000 and audio.num_channels == 1
