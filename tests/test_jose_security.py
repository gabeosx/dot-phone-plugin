"""Reject unsigned JWS data and oversized compact input before copying segments."""

from copy import deepcopy

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


def test_jwe_recipient_limit_rejects_excessive_recipient_list():
    from authlib.jose import JsonWebEncryption

    verifier = JsonWebEncryption()
    key = b"fictional-jwe-regression-key".ljust(32, b".")
    payload = b'{"synthetic":true}'
    signed = verifier.serialize_json(
        {"protected": {"alg": "A256KW", "enc": "A256GCM"}}, payload, key
    )
    assert verifier.deserialize_json(signed, key)["payload"] == payload
    signed["recipients"] = [deepcopy(signed["recipients"][0]) for _ in range(17)]

    with pytest.raises(ValueError, match="Too many recipients"):
        verifier.deserialize_json(signed, key)


def test_registration_rejects_private_jwks_before_key_import(monkeypatch):
    from authlib.oauth2.rfc7591.claims import ClientMetadataClaims
    from joserfc.errors import InvalidClaimError
    from joserfc.jwk import KeySet, RSAKey

    key = RSAKey.generate_key(2048, private=True)
    public_jwks = {"keys": [key.as_dict(private=False)]}
    ClientMetadataClaims({"jwks": public_jwks}, {}).validate_jwks()
    private_key = key.as_dict(private=True)
    minimal_private_key = {name: private_key[name] for name in ("kty", "n", "e", "d")}

    def refuse_private_key_import(*args, **kwargs):
        raise AssertionError("Private registration key reached key import")

    monkeypatch.setattr(KeySet, "import_key_set", refuse_private_key_import)
    for value in (private_key, minimal_private_key):
        with pytest.raises(InvalidClaimError, match="jwks"):
            ClientMetadataClaims({"jwks": {"keys": [value]}}, {}).validate_jwks()
