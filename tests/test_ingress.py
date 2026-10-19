import httpx
import pytest
from fastapi.testclient import TestClient

from dots_phone.ingress import allowed, create_gateway


@pytest.mark.parametrize(
    "path",
    [
        "/mcp",
        "/authorize",
        "/token",
        "/approve/" + "a" * 32,
        "/owner/campaign/audio",
        "/owner/campaign/phone/" + "a" * 32,
        "/owner/campaign/phone/" + "a" * 32 + "/revoke",
    ],
)
def test_media_gateway_never_exposes_mcp_or_management(path):
    with TestClient(create_gateway("media")) as client:
        assert client.get(path).status_code == 404
        assert client.post(path).status_code == 404


def test_owner_gateway_excludes_mcp_and_carrier_routes():
    assert allowed("owner", "GET", "/authorize")
    assert allowed("owner", "POST", "/token")
    assert allowed("owner", "GET", "/approve/" + "a" * 32)
    assert not allowed("owner", "POST", "/mcp")
    assert not allowed("owner", "POST", "/twilio/status/" + "a" * 32)
    assert not allowed("media", "POST", "/twilio/status/not-a-call")


def test_owner_gateway_accepts_the_actual_oauth_transaction_id():
    import secrets

    transaction = secrets.token_urlsafe(32)
    assert len(transaction) == 43
    for method in ["GET", "POST"]:
        assert allowed("owner", method, "/owner/consent/" + transaction)
        assert not allowed("media", method, "/owner/consent/" + transaction)
        assert not allowed("owner", method, "/owner/consent/" + transaction + "/mcp")
        assert not allowed("owner", method, "/owner/consent/" + "a" * 32)
    assert not allowed("owner", "DELETE", "/owner/consent/" + transaction)


def test_gateway_preserves_signature_and_refuses_oversize_bodies():
    observed = []

    def upstream(request):
        observed.append(request)
        return httpx.Response(204)

    remote = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    with TestClient(create_gateway("media", remote)) as client:
        url = "/twilio/status/" + "a" * 32
        assert (
            client.post(
                url, content="fixture", headers={"x-twilio-signature": "fixture-signature"}
            ).status_code
            == 204
        )
        assert observed[0].headers["x-twilio-signature"] == "fixture-signature"
        assert observed[0].url.host == "phone-app"
        assert client.post(url, content="x" * 32769).status_code == 413
        assert len(observed) == 1
