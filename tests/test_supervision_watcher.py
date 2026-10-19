from experiments.pipecat.watch_supervised_call import decision_was_forwarded


def test_fast_forwarding_does_not_require_observing_the_pending_snapshot():
    snapshot = {
        "state": "active",
        "revision": 1,
        "progress": "Scoped answer forwarded; playback unconfirmed.",
    }
    assert decision_was_forwarded(snapshot, None)
    assert decision_was_forwarded(snapshot, {"revision": 1})
    assert not decision_was_forwarded(snapshot, {"revision": 2})


def test_holding_audio_or_terminal_state_does_not_mark_forwarding():
    snapshot = {
        "state": "awaiting_decision",
        "revision": 1,
        "progress": "Waiting for a scoped Dots decision.",
    }
    assert not decision_was_forwarded(snapshot, None)
    snapshot.update(state="completed", progress="Scoped answer forwarded; playback unconfirmed.")
    assert not decision_was_forwarded(snapshot, None)
