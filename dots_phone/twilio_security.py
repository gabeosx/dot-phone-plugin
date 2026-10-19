"""Fixed-origin Media Stream signatures and exact carrier-state retrieval."""

from twilio.base.exceptions import TwilioRestException


def valid_media_signature(validator, origin, path, signature):
    if not origin.startswith("https://") or not path.startswith("/") or not signature:
        return False
    # Voice WSS handshakes may sign the configured WSS URL and normalize a
    # trailing slash. Only this configured origin and exact route are accepted.
    url = origin.rstrip("/") + path
    return any(
        validator.validate(candidate, {}, signature)
        for candidate in (
            url,
            url + "/",
            url.replace("https://", "wss://", 1),
            url.replace("https://", "wss://", 1) + "/",
        )
    )


def fetch_exact_call(client, call_sid):
    try:
        return client.calls(call_sid).fetch()
    except TwilioRestException as error:
        if error.code != 8012:
            raise
        # A restricted key can list calls while the individual GET responds
        # "permission resource model missing", despite calls/read being present.
        # Match the exact SID in an authoritative list using the same credential.
        for call in client.calls.list(limit=100):
            if call.sid == call_sid:
                return call
        raise RuntimeError("exact carrier call was absent from the authorized list") from None
