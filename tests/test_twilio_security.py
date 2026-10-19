from types import SimpleNamespace

import pytest
from twilio.base.exceptions import TwilioRestException
from twilio.request_validator import RequestValidator

from dots_phone.twilio_security import fetch_exact_call, valid_media_signature


def test_media_signature_accepts_only_fixed_origin_and_route_variants():
    validator = RequestValidator("fixture-token")
    origin = "https://media.example"
    for url in [
        "https://media.example/voice",
        "https://media.example/voice/",
        "wss://media.example/voice",
        "wss://media.example/voice/",
    ]:
        signature = validator.compute_signature(url, {})
        assert valid_media_signature(validator, origin, "/voice", signature)
    for url in [
        "https://other.example/voice",
        "https://media.example/other",
        "https://media.example/voice?admin=true",
    ]:
        signature = validator.compute_signature(url, {})
        assert not valid_media_signature(validator, origin, "/voice", signature)
    assert not valid_media_signature(validator, origin, "/voice", "")


def test_restricted_key_status_fallback_matches_exact_call_and_fails_closed():
    expected = SimpleNamespace(sid="CAexpected", status="completed")

    class Calls:
        def __call__(self, sid):
            return self

        def fetch(self):
            raise TwilioRestException(401, "https://carrier.example/call", code=8012)

        def list(self, limit):
            assert limit == 100
            return [SimpleNamespace(sid="CAother", status="in-progress"), expected]

    client = SimpleNamespace(calls=Calls())
    assert fetch_exact_call(client, "CAexpected") is expected
    with pytest.raises(RuntimeError):
        fetch_exact_call(client, "CAmissing")
