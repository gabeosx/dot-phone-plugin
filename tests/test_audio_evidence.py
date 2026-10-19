"""Local audio observations preserve source identity and retention boundaries."""

import asyncio
import base64
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from dots_phone import audio_evidence
from dots_phone.audio_evidence import (
    AudioEvidenceUnavailable,
    audio_catalog,
    prepare_audio_evidence,
    recipient_pcm,
    recording_digest,
    recording_snapshot,
)
from dots_phone.call_evidence import CallRecording


def archive(service, brief, *, finished=True):
    cid = service.prepare("owner", brief)["call_id"]
    call = service.call("owner", cid)
    clock = [0.0]
    recording = CallRecording(service, call, clock=lambda: clock[0])
    for direction, value in (("recipient", 0), ("assistant", 128)):
        recording.observe(
            json.dumps(
                {
                    "event": "media",
                    "media": {"payload": base64.b64encode(bytes([value]) * 160).decode()},
                }
            ),
            direction,
        )
    clock[0] = 1.0
    if finished:
        recording.finish()
    return cid, recording


def enable(service, tmp_path):
    directory = tmp_path / "cached-small.en"
    directory.mkdir(exist_ok=True)
    (directory / "model.bin").write_bytes(b"small-model-fixture")
    service.settings.post_call_audio_model_directory = directory
    return directory


async def test_optional_recognizer_is_disabled_without_reading_private_audio(service, brief):
    cid = service.prepare("owner", brief)["call_id"]
    result = await prepare_audio_evidence(service, service.call("owner", cid))
    assert result == {"status": "disabled", "reason": "local_recognizer_not_configured"}


async def test_recording_disabled_cannot_trigger_transcription_or_archive_read(
    service, brief, tmp_path, monkeypatch
):
    enable(service, tmp_path)
    cid, _ = archive(service, brief)
    call = service.call("owner", cid)
    call["evidence_policy"]["recording"] = False
    monkeypatch.setattr(
        audio_evidence, "recording_snapshot", lambda *_: pytest.fail("archive read")
    )
    monkeypatch.setattr(
        audio_evidence, "transcribe_recipient", lambda *_: pytest.fail("recognition")
    )
    assert (await prepare_audio_evidence(service, call))["reason"] == "recording_retention_disabled"


async def test_recognizer_receives_only_recipient_pcm_and_no_claim_or_native_asr(
    service, brief, tmp_path, monkeypatch
):
    directory = enable(service, tmp_path)
    cid, _ = archive(service, brief)
    call = service.call("owner", cid)
    call["transcript"] = [{"role": "user", "text": "Wrong native amount: 12300"}]
    service.save(call)
    expected = recipient_pcm(recording_snapshot(service, call))
    observed = []

    def transcribe(*args):
        observed.append(args)
        return [{"start_ms": 0, "end_ms": 20, "text": "Your available balance is 12,340 points."}]

    monkeypatch.setattr(audio_evidence, "transcribe_recipient", transcribe)
    result = await prepare_audio_evidence(service, call)
    assert observed == [(expected, directory)]
    assert result["status"] == "available"
    assert result["recognizer"]["claimed_value_or_transcript_prompt"] is False
    assert result["source"]["boundary"] == "recipient_audio_received_from_carrier"
    assert len(result["source"]["recipient_pcm_sha256"]) == 64
    assert service.call("owner", cid)["transcript"] == call["transcript"]
    # The assistant's opposite-sign samples did not enter the input channel.
    from array import array

    samples = array("h")
    samples.frombytes(expected)
    assert samples[0] < 0


async def test_waits_boundedly_for_recording_finalization_after_voice_closure(
    service, brief, tmp_path, monkeypatch
):
    enable(service, tmp_path)
    cid, recorder = archive(service, brief, finished=False)
    monkeypatch.setattr(audio_evidence, "transcribe_recipient", lambda *_: [])

    async def finalize():
        await asyncio.sleep(0.01)
        recorder.finish()

    task = asyncio.create_task(finalize())
    result = await prepare_audio_evidence(service, service.call("owner", cid))
    await task
    assert result["status"] == "available"


@pytest.mark.parametrize("state", [None, "capturing", "incomplete"])
async def test_missing_or_incomplete_recording_falls_back_without_recognizing(
    service, brief, tmp_path, monkeypatch, state
):
    enable(service, tmp_path)
    cid = service.prepare("owner", brief)["call_id"]
    call = service.call("owner", cid)
    if state:
        call["recording"] = {"state": state}
        service.save(call)
    monkeypatch.setattr(audio_evidence, "RECORDING_WAIT_SECONDS", 0.01)
    monkeypatch.setattr(
        audio_evidence, "transcribe_recipient", lambda *_: pytest.fail("recognition")
    )
    result = await prepare_audio_evidence(service, call)
    assert result["status"] == "unavailable"
    assert result["reason"] in {"recording_incomplete", "recording_finalization_unavailable"}


async def test_missing_chunk_is_not_presented_as_a_complete_observation(
    service, brief, tmp_path, monkeypatch
):
    enable(service, tmp_path)
    cid, _ = archive(service, brief)
    service.store.delete("call_audio", f"{cid}:000000")
    monkeypatch.setattr(
        audio_evidence, "transcribe_recipient", lambda *_: pytest.fail("recognition")
    )
    result = await prepare_audio_evidence(service, service.call("owner", cid))
    assert result == {"status": "unavailable", "reason": "recording_chunk_unavailable"}


def test_source_and_citations_bind_waveform_origin_and_segment_position(service, brief):
    cid, _ = archive(service, brief)
    call = service.call("owner", cid)
    before = recording_digest(service, call)
    chunk = service.store.get("call_audio", f"{cid}:000000", "owner")
    chunk["packets"][0]["payload"] = base64.b64encode(bytes([1]) * 160).decode()
    service.store.put("call_audio", f"{cid}:000000", "owner", chunk)
    assert recording_digest(service, call) != before
    observation = {
        "status": "available",
        "source": {"recording_digest": before, "recipient_pcm_sha256": "a" * 64},
        "recognizer": dict(audio_evidence.RECOGNIZER),
        "segments": [{"start_ms": 0, "end_ms": 20, "text": "Available balance is 184 points."}],
    }
    first = audio_catalog(observation)[0]
    observation["segments"][0]["start_ms"] = 1
    assert first["id"] != audio_catalog(observation)[0]["id"]
    observation["source"]["recipient_pcm_sha256"] = "b" * 64
    assert first["id"] != audio_catalog(observation)[0]["id"]
    assert first["origin"] == "independent_local_audio_transcription"


async def test_local_recognition_timeout_is_honest_fallback(service, brief, tmp_path, monkeypatch):
    enable(service, tmp_path)
    cid, _ = archive(service, brief)
    release = threading.Event()

    def recognize(*_):
        release.wait(timeout=1)
        return []

    monkeypatch.setattr(audio_evidence, "RECOGNITION_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(audio_evidence, "transcribe_recipient", recognize)
    try:
        result = await prepare_audio_evidence(service, service.call("owner", cid))
        assert result["status"] == "unavailable"
        assert result["reason"] == "local_recognition_timed_out"
        # The timed-out native job is still running. A later review gets an
        # honest fallback instead of queueing another copy of private audio.
        again = await prepare_audio_evidence(service, service.call("owner", cid))
        assert again == {"status": "unavailable", "reason": "local_recognizer_busy"}
    finally:
        release.set()


def test_local_model_path_must_exist_and_never_names_a_remote_model():
    with pytest.raises(AudioEvidenceUnavailable, match="local_model_unavailable"):
        audio_evidence.transcribe_recipient(bytes(160), Path("/missing/offline-model-fixture"))


def test_recognizer_constructor_and_inference_are_offline_unprimed_and_bounded(
    tmp_path, monkeypatch
):
    (tmp_path / "model.bin").write_bytes(b"fake weights for constructor contract test")
    seen = {}

    class LocalModel:
        def __init__(self, directory, **kwargs):
            seen["directory"], seen["constructor"] = directory, kwargs

        def transcribe(self, samples, **kwargs):
            seen["samples"], seen["inference"] = samples, kwargs
            return iter(
                [SimpleNamespace(start=0, end=0.02, text=" Unprompted observation. ")]
            ), None

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=LocalModel))
    audio_evidence._local_model.cache_clear()
    try:
        segments = audio_evidence.transcribe_recipient(bytes(320), tmp_path)
        assert seen["directory"] == str(tmp_path.resolve())
        assert seen["constructor"] == {
            "device": "cpu",
            "compute_type": "int8",
            "cpu_threads": 2,
            "num_workers": 1,
            "local_files_only": True,
        }
        assert seen["inference"] == {
            "language": "en",
            "beam_size": 5,
            "vad_filter": False,
            "condition_on_previous_text": False,
        }
        assert len(seen["samples"].shape) == 1
        assert seen["samples"].shape[0] in (319, 320)
        assert segments == [{"start_ms": 0, "end_ms": 20, "text": "Unprompted observation."}]
    finally:
        audio_evidence._local_model.cache_clear()


def observation(texts):
    return {
        "status": "available",
        "source": {"recording_digest": "a" * 64, "recipient_pcm_sha256": "b" * 64},
        "recognizer": dict(audio_evidence.RECOGNIZER),
        "segments": [
            {"start_ms": index * 1000, "end_ms": (index + 1) * 1000, "text": text}
            for index, text in enumerate(texts)
        ],
    }


@pytest.mark.parametrize(
    "texts",
    [
        [
            "The reimbursement is eighteen dollars",
            "and five cents.",
            "It excludes the pending adjustment.",
        ],
        [
            "Your parcel should leave on Tuesday.",
            "Delivery is expected Friday after three.",
            "A signature is required.",
        ],
        ["The rate is sixty dollars per night.", "Tax is included.", "Breakfast costs extra."],
    ],
)
def test_contiguous_windows_join_arbitrary_asr_fragments_without_claim_knowledge(texts):
    catalog = audio_catalog(observation(texts))
    individual = [item for item in catalog if "excerpt_kind" not in item]
    windows = [item for item in catalog if item.get("excerpt_kind") == "contiguous_window"]
    assert [item["text"] for item in individual] == texts
    combined = next(item for item in windows if item["start_ms"] == 0)
    assert combined["text"] == " ".join(texts)
    assert combined["source_evidence_ids"] == [item["id"] for item in individual]
    assert combined["segment_parts"] == [
        {"segment_index": index, "segment_part": 0} for index in range(len(texts))
    ]
    assert combined["end_ms"] == len(texts) * 1000
    assert all(len(item["text"]) <= 240 for item in catalog)


def test_windows_never_skip_a_correction_to_join_matching_fragments():
    texts = [
        "Your available balance is 184 points.",
        "Correction: that amount is pending, not available.",
        "The current available balance is zero.",
        "Pending points are excluded.",
    ]
    catalog = audio_catalog(observation(texts))
    individual = [item for item in catalog if "excerpt_kind" not in item]
    positions = {item["id"]: index for index, item in enumerate(individual)}
    assert [item["text"] for item in individual] == texts
    for window in (item for item in catalog if item.get("excerpt_kind") == "contiguous_window"):
        indexes = [positions[reference] for reference in window["source_evidence_ids"]]
        assert indexes == list(range(indexes[0], indexes[-1] + 1))
        if indexes[0] == 0 and indexes[-1] >= 2:
            assert "Correction:" in window["text"]
    assert any("Correction:" in item["text"] and "184" in item["text"] for item in catalog)


def test_window_budget_does_not_remove_original_long_segments_or_later_corrections():
    texts = [
        "The operator explains the route and its conditions. " * 7,
        "An earlier estimate mentioned Tuesday, subject to confirmation.",
        "Correction: dispatch is now Friday.",
        "Delivery takes two business days after dispatch.",
    ]
    catalog = audio_catalog(observation(texts))
    individual = [item for item in catalog if "excerpt_kind" not in item]
    assert (
        " ".join(item["text"] for item in individual if item["segment_index"] == 0)
        == texts[0].strip()
    )
    assert any(item["text"] == texts[2] for item in individual)
    assert len(catalog) <= 2 * len(individual)
    assert all(0 < len(item["text"]) <= 240 for item in catalog)
    assert any(texts[2] in item["text"] and texts[3] in item["text"] for item in catalog)


def enable_confirmation(service, tmp_path):
    directory = tmp_path / "cached-medium.en"
    directory.mkdir(exist_ok=True)
    (directory / "model.bin").write_bytes(b"medium-model-fixture")
    service.settings.post_call_audio_confirmation_model_directory = directory
    return directory


async def test_optional_recognizers_receive_identical_canonical_pcm_in_serial_order(
    service, brief, tmp_path, monkeypatch
):
    primary = enable(service, tmp_path)
    confirmation = enable_confirmation(service, tmp_path)
    cid, _ = archive(service, brief)
    canonical = recipient_pcm(recording_snapshot(service, service.call("owner", cid)))
    received = []

    def recognize(pcm, directory):
        received.append((pcm, directory))
        text = (
            "Primary observation is 12300."
            if directory == primary
            else "Confirmation observation is 12340."
        )
        return [{"start_ms": 0, "end_ms": 20, "text": text}]

    monkeypatch.setattr(audio_evidence, "transcribe_recipient", recognize)
    result = await prepare_audio_evidence(service, service.call("owner", cid))
    second = result["additional_observations"][0]
    assert received == [(canonical, primary), (canonical, confirmation)]
    assert result["status"] == second["status"] == "available"
    assert result["source"] == second["source"]
    assert result["recognizer"]["model"] == "small.en"
    assert second["recognizer"]["model"] == "medium.en"
    assert result["recognizer"]["model_revision"].startswith("sha256:")
    assert result["recognizer"]["model_revision"] != second["recognizer"]["model_revision"]
    assert result["segments"][0]["text"] == "Primary observation is 12300."
    assert second["segments"][0]["text"] == "Confirmation observation is 12340."
    catalog = audio_catalog(result)
    assert {item["recognizer"]["model"] for item in catalog} == {"small.en", "medium.en"}
    assert {item["audio_sha256"] for item in catalog} == {result["source"]["recipient_pcm_sha256"]}


@pytest.mark.parametrize("failed_model", ["primary", "confirmation"])
async def test_each_failed_recognizer_preserves_other_completed_observation(
    service, brief, tmp_path, monkeypatch, failed_model
):
    primary = enable(service, tmp_path)
    confirmation = enable_confirmation(service, tmp_path)
    cid, _ = archive(service, brief)
    failed = primary if failed_model == "primary" else confirmation

    def recognize(_pcm, directory):
        if directory == failed:
            raise RuntimeError("local decoder failed")
        return [{"start_ms": 0, "end_ms": 20, "text": "Completed independent observation."}]

    monkeypatch.setattr(audio_evidence, "transcribe_recipient", recognize)
    result = await prepare_audio_evidence(service, service.call("owner", cid))
    observed = audio_evidence.observations(result)
    assert [item["status"] for item in observed] == (
        ["unavailable", "available"] if failed_model == "primary" else ["available", "unavailable"]
    )
    assert (
        next(item for item in observed if item["status"] == "unavailable")["reason"]
        == "local_recognition_failed"
    )
    assert len(audio_catalog(result)) == 1


async def test_confirmation_timeout_preserves_primary_and_cannot_later_mutate_snapshot(
    service, brief, tmp_path, monkeypatch
):
    primary = enable(service, tmp_path)
    enable_confirmation(service, tmp_path)
    cid, _ = archive(service, brief)
    release, completed = threading.Event(), threading.Event()

    def recognize(_pcm, directory):
        if directory != primary:
            release.wait(timeout=1)
            completed.set()
        return [{"start_ms": 0, "end_ms": 20, "text": "A completed observation."}]

    monkeypatch.setattr(audio_evidence, "transcribe_recipient", recognize)
    monkeypatch.setattr(audio_evidence, "RECOGNITION_TIMEOUT_SECONDS", 0.05)
    try:
        result = await prepare_audio_evidence(service, service.call("owner", cid))
        assert result["status"] == "available"
        assert result["additional_observations"][0]["reason"] == "local_recognition_timed_out"
        frozen = json.dumps(result, sort_keys=True)
    finally:
        release.set()
    await asyncio.to_thread(completed.wait, 1)
    assert json.dumps(result, sort_keys=True) == frozen


def test_additional_windows_keep_recognizers_separate_and_summaries_strip_nested_text():
    primary = observation(["First observation amount.", "First observation qualification."])
    additional = observation(["Other observation amount.", "Other observation qualification."])
    additional["recognizer"] = {**additional["recognizer"], "model": "medium.en"}
    primary["additional_observations"] = [additional]
    catalog = audio_catalog(primary)
    by_id = {item["id"]: item for item in catalog}
    for item in catalog:
        for reference in item.get("source_evidence_ids", []):
            assert by_id[reference]["recognizer"] == item["recognizer"]
    summary = audio_evidence.observation_summary(primary)
    assert "segments" not in summary
    assert "segments" not in summary["additional_observations"][0]
    assert "Other observation" not in str(summary)


def test_model_revision_identifies_local_weights_and_changes_with_them(tmp_path):
    weights = tmp_path / "model.bin"
    weights.write_bytes(b"one fixture revision")
    original = audio_evidence.model_revision(tmp_path)
    weights.write_bytes(b"a different fixture revision")
    assert audio_evidence.model_revision(tmp_path) != original
