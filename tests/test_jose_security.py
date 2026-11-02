"""Reject unsigned JWS data and oversized compact input before copying segments."""

import pytest
from authlib.jose import JsonWebSignature
from authlib.jose.errors import JoseError
from joserfc.errors import ExceededSizeError
from joserfc.jws import deserialize_compact


@pytest.mark.parametrize("verification_key", [None, b"fictional-jws-regression-key-32-bytes"])
def test_empty_signature_list_cannot_authenticate_a_payload(verification_key):
    verifier = JsonWebSignature()
    key = b"fictional-jws-regression-key-32-bytes"
    payload = b'{"synthetic":true}'
    signed = verifier.serialize_json({"protected": {"alg": "HS256"}}, payload, key)
    assert verifier.deserialize_json(signed, key)["payload"] == payload
    with pytest.raises(JoseError):
        verifier.deserialize_json({"payload": "e30", "signatures": []}, verification_key)


def test_compact_size_limit_rejects_before_allocating_split_segments():
    class NoSegmentCopies(bytes):
        def split(self, *args, **kwargs):
            raise AssertionError("Oversized input was split before the size limit was enforced")

    value = NoSegmentCopies(b"A" * (2 * 1024 * 1024))
    with pytest.raises(ExceededSizeError):
        deserialize_compact(value, public_key=None)
