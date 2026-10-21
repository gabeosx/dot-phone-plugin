import copy
import json
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("pipecat")

from pipecat.services.openai.realtime import events

from dots_phone.pipecat_realtime import SupervisorSocket, sdk_session_message


class Socket:
    def __init__(self, messages):
        self.messages = messages

    async def __aiter__(self):
        for message in self.messages:
            yield message


@pytest.mark.parametrize("kind", ["session.created", "session.updated"])
@pytest.mark.parametrize("model", ["gpt-transcribe", "gpt-realtime-whisper"])
@pytest.mark.parametrize(
    "metadata",
    [
        {"languages": ["en"]},
        {"languages": None},
        {"keywords": []},
        {"languages": [], "keywords": ["rewards"], "delay": None},
        {"delay": "medium"},
    ],
)
async def test_provider_session_metadata_decodes_without_changing_raw_observation(
    kind, model, metadata
):
    transcription = {"model": model, "language": None, "prompt": None, **metadata}
    event = {
        "type": kind,
        "event_id": "fixture-event",
        "session": {
            "id": "fixture-session",
            "model": "gpt-realtime-2.1",
            "audio": {"input": {"transcription": transcription}},
        },
    }
    original = copy.deepcopy(event)
    encoded = json.dumps(event)
    # Regression: the pinned SDK's custom constructor rejects the new fields.
    with pytest.raises(Exception, match="unexpected keyword argument"):
        events.parse_server_event(encoded)
    observer = AsyncMock()
    [adapted] = [message async for message in SupervisorSocket(Socket([encoded]), observer)]
    decoded = events.parse_server_event(adapted)
    assert decoded.session.audio.input.transcription.model == model
    assert decoded.session.audio.input.transcription.language is None
    assert decoded.session.audio.input.transcription.prompt is None
    observer.assert_awaited_once_with(original)
    assert event == original
    assert observer.call_args.args[0] == original


@pytest.mark.parametrize(
    "metadata",
    [
        {"languages": "en"},
        {"languages": [1]},
        {"keywords": {}},
        {"delay": "unknown"},
        {"delay": []},
        {"delay": True},
    ],
)
def test_invalid_new_session_metadata_fails_instead_of_being_silently_discarded(metadata):
    event = {
        "type": "session.updated",
        "session": {"audio": {"input": {"transcription": {"model": "gpt-transcribe", **metadata}}}},
    }
    with pytest.raises(ValueError, match="acknowledged transcription"):
        sdk_session_message(event, json.dumps(event))


def test_adapter_keeps_unknown_fields_and_does_not_modify_transcription_or_audio_events():
    unknown = {
        "type": "session.updated",
        "session": {
            "audio": {"input": {"transcription": {"model": "gpt-transcribe", "unknown": True}}}
        },
    }
    encoded = json.dumps(unknown)
    assert sdk_session_message(unknown, encoded) == encoded
    with pytest.raises(Exception, match="unexpected keyword argument"):
        events.parse_server_event(encoded)
    for event in [
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "transcript": "12,340",
            "languages": [{"code": "en"}],
        },
        {"type": "response.output_audio.delta", "delta": "unchanged-audio"},
    ]:
        encoded = json.dumps(event, indent=2)
        assert sdk_session_message(event, encoded) == encoded
