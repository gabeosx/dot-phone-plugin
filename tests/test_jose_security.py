"""Reject unsigned JWS data and oversized compact input before copying segments."""

import pytest
from authlib.jose import JsonWebSignature
from authlib.jose.errors import JoseError
from authlib.jose.rfc7518 import OctKey
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, load_ssh_public_key
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


def test_ssh_public_key_with_tab_separator_cannot_be_an_hmac_secret():
    public_key = Ed25519PrivateKey.generate().public_key()
    ssh_key = public_key.public_bytes(Encoding.OpenSSH, PublicFormat.OpenSSH)
    tab_separated_key = ssh_key.replace(b" ", b"\t", 1)
    assert load_ssh_public_key(tab_separated_key) == public_key
    with pytest.raises(ValueError):
        OctKey.import_key(tab_separated_key)
