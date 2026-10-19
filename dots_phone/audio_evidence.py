"""Independent, local observations of retained recipient audio after a call.

Recognition is supplementary evidence, never a second turn detector or an action
gate. The recognizer receives only PCM, not the proposal or native transcription.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import threading
from array import array
from functools import lru_cache
from pathlib import Path

from dots_phone.call_evidence import render_recording
from dots_phone.call_task import recipient_evidence

RECORDING_WAIT_SECONDS = 3.0
RECOGNITION_TIMEOUT_SECONDS = 45.0
_RECOGNITION_LOCK = threading.Lock()
RECOGNIZER = {
    "name": "faster-whisper",
    "model": "small.en",
    "device": "cpu",
    "compute_type": "int8",
    "claimed_value_or_transcript_prompt": False,
    "local_files_only": True,
}


class AudioEvidenceUnavailable(ValueError):
    pass


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def recording_snapshot(service, call):
    """Read one complete owner-bound encrypted archive, without exporting files."""
    if not call.get("evidence_policy", {}).get("recording", service.settings.retain_recording):
        raise AudioEvidenceUnavailable("recording_retention_disabled")
    metadata = call.get("recording")
    if not metadata or metadata.get("state") != "complete":
        raise AudioEvidenceUnavailable("complete_recording_unavailable")
    count, duration = metadata.get("chunks"), metadata.get("duration_ms")
    if (
        type(count) is not int
        or not 0 <= count <= 8192
        or type(duration) is not int
        or not 0 < duration <= (call["brief"]["max_seconds"] + 30) * 1000
        or metadata.get("format") != "pcmu/8000/mono-per-direction"
    ):
        raise AudioEvidenceUnavailable("invalid_recording_manifest")
    chunks = []
    for index in range(count):
        chunk = service.store.get("call_audio", f"{call['call_id']}:{index:06d}", call["owner"])
        if not chunk or chunk.get("call_id") != call["call_id"] or chunk.get("index") != index:
            raise AudioEvidenceUnavailable("recording_chunk_unavailable")
        chunks.append(chunk)
    return {
        "metadata": metadata,
        "chunks": chunks,
        "digest": _digest({"metadata": metadata, "chunks": chunks}),
    }


def recording_digest(service, call):
    """Re-read source identity to reject evidence changed during model review."""
    return recording_snapshot(service, call)["digest"]


def recipient_pcm(snapshot):
    # render_recording validates contiguous chunk indexes and retains packet
    # timing. Recipient and assistant are separate carrier directions.
    stereo = array("h")
    stereo.frombytes(render_recording(snapshot["metadata"], snapshot["chunks"]))
    return stereo[0::2].tobytes()


@lru_cache(maxsize=4)
def _revision_for_files(files):
    digests = {}
    for name, path, _size, _modified in files:
        with open(path, "rb") as stream:
            digests[name] = hashlib.file_digest(stream, "sha256").hexdigest()
    return "sha256:" + _digest(digests)


def model_revision(directory):
    """Identify the actual local weights/tokenizer/config without model priming."""
    path = Path(directory)
    if not path.is_dir() or not (path / "model.bin").is_file():
        raise AudioEvidenceUnavailable("local_model_unavailable")
    files = []
    for name in ("config.json", "model.bin", "tokenizer.json", "vocabulary.json", "vocabulary.txt"):
        file = path / name
        if file.is_file():
            stat = file.stat()
            files.append((name, str(file.resolve()), stat.st_size, stat.st_mtime_ns))
    return _revision_for_files(tuple(files))


@lru_cache(maxsize=2)
def _local_model(directory, revision):
    from faster_whisper import WhisperModel

    return WhisperModel(
        directory,
        device="cpu",
        compute_type="int8",
        cpu_threads=2,
        num_workers=1,
        local_files_only=True,
    )


def transcribe_recipient(pcm, directory):
    """No initial prompt, native ASR, report, source text, network or temp WAV."""
    path = Path(directory)
    if not path.is_dir():
        raise AudioEvidenceUnavailable("local_model_unavailable")
    import audioop

    import numpy as np

    resampled, _ = audioop.ratecv(pcm, 2, 1, 8000, 16000, None)
    samples = np.frombuffer(resampled, dtype=np.int16).astype(np.float32) / 32768
    segments, _ = _local_model(str(path.resolve()), model_revision(path)).transcribe(
        samples,
        language="en",
        beam_size=5,
        vad_filter=False,
        condition_on_previous_text=False,
    )
    return [
        {
            "start_ms": round(segment.start * 1000),
            "end_ms": round(segment.end * 1000),
            "text": segment.text.strip(),
        }
        for segment in segments
    ]


def _validate_segments(segments):
    # Only protocol bounds: the reasoner judges amounts, words and conditions.
    if not isinstance(segments, list) or len(segments) > 4096:
        raise AudioEvidenceUnavailable("invalid_recognizer_output")
    for segment in segments:
        if (
            not isinstance(segment, dict)
            or not isinstance(segment.get("text"), str)
            or len(segment["text"]) > 16000
            or any(
                type(segment.get(key)) not in (int, float) or not math.isfinite(segment[key])
                for key in ("start_ms", "end_ms")
            )
            or not 0 <= segment["start_ms"] <= segment["end_ms"]
        ):
            raise AudioEvidenceUnavailable("invalid_recognizer_output")


def _recognize_once(pcm, configured, source, completed, stop):
    # A timed-out native inference cannot be forcibly killed by asyncio. Keep
    # at most one local job, rather than accumulating queued CPU work or audio.
    if not _RECOGNITION_LOCK.acquire(blocking=False):
        raise AudioEvidenceUnavailable("local_recognizer_busy")
    try:
        for directory, name in configured:
            if stop.is_set():
                return
            recognizer = {**RECOGNIZER, "model": name}
            try:
                recognizer["model_revision"] = model_revision(directory)
                segments = transcribe_recipient(pcm, directory)
                _validate_segments(segments)
                observation = {
                    "status": "available",
                    "source": source,
                    "recognizer": recognizer,
                    "segments": segments,
                }
            except AudioEvidenceUnavailable as error:
                observation = {
                    "status": "unavailable",
                    "reason": str(error),
                    "source": source,
                    "recognizer": recognizer,
                }
            except Exception:
                observation = {
                    "status": "unavailable",
                    "reason": "local_recognition_failed",
                    "source": source,
                    "recognizer": recognizer,
                }
            # Publish completed work before starting the optional confirmation.
            # The caller snapshots this on timeout; late work cannot mutate it.
            completed.append(observation)
    finally:
        _RECOGNITION_LOCK.release()


def single_audio_catalog(observation):
    """Keep individual observations and bounded, contiguous citation windows.

    ASR segment boundaries are acoustic bookkeeping, not semantic boundaries.
    A claim may depend on the amount in one segment and its conditions in the
    next. Windows preserve every intervening observation; they never choose
    wording based on the report, value or whether it looks like a correction.
    """
    if not observation or observation.get("status") != "available":
        return []
    result = []
    for index, segment in enumerate(observation.get("segments", [])):
        for part, excerpt in enumerate(
            recipient_evidence([{"role": "user", "text": segment["text"]}])
        ):
            item = {
                "text": excerpt["text"],
                "origin": "independent_local_audio_transcription",
                "source_digest": observation["source"]["recording_digest"],
                "audio_sha256": observation["source"]["recipient_pcm_sha256"],
                "segment_index": index,
                "segment_part": part,
                "start_ms": segment["start_ms"],
                "end_ms": segment["end_ms"],
                "timing_basis": "milliseconds_from_retained_recording_start",
                "recognizer": observation["recognizer"],
            }
            result.append({"id": _digest(item)[:16], **item})
    windows = []
    for start, first in enumerate(result):
        pieces, text = [first], first["text"]
        for following_index in range(start + 1, len(result)):
            following = result[following_index]
            joined = text + " " + following["text"]
            if len(joined) > 240:
                break
            pieces.append(following)
            text = joined
        if len(pieces) < 2:
            continue
        item = {
            "text": text,
            "origin": first["origin"],
            "source_digest": first["source_digest"],
            "audio_sha256": first["audio_sha256"],
            "excerpt_kind": "contiguous_window",
            "source_evidence_ids": [piece["id"] for piece in pieces],
            "segment_parts": [
                {"segment_index": piece["segment_index"], "segment_part": piece["segment_part"]}
                for piece in pieces
            ],
            "start_ms": first["start_ms"],
            "end_ms": max(piece["end_ms"] for piece in pieces),
            "timing_basis": first["timing_basis"],
            "recognizer": first["recognizer"],
        }
        windows.append({"id": _digest(item)[:16], **item})
    return [*result, *windows]


def observations(observation):
    if not observation:
        return []
    return [observation, *observation.get("additional_observations", [])]


def audio_catalog(observation):
    """Each recognizer has its own catalog/windows over the same source audio."""
    return [
        item for observed in observations(observation) for item in single_audio_catalog(observed)
    ]


def observation_summary(observation):
    """Non-transcript provenance can be retained when transcript retention is off."""
    summary = {
        key: value
        for key, value in observation.items()
        if key not in {"segments", "additional_observations"}
    }
    if "additional_observations" in observation:
        summary["additional_observations"] = [
            observation_summary(additional) for additional in observation["additional_observations"]
        ]
    return summary


async def prepare_audio_evidence(service, call):
    """Wait only after closure, then independently observe permitted retained audio.

    A timeout stops waiting for the CPU worker; it never resumes or retains a
    telephone connection. No full derived transcription is persisted here.
    """
    if not call.get("evidence_policy", {}).get("recording", service.settings.retain_recording):
        return {"status": "disabled", "reason": "recording_retention_disabled"}
    directory = getattr(service.settings, "post_call_audio_model_directory", None)
    if directory is None:
        return {"status": "disabled", "reason": "local_recognizer_not_configured"}
    deadline = asyncio.get_running_loop().time() + RECORDING_WAIT_SECONDS
    while True:
        current = service.call(call["owner"], call["call_id"])
        state = (current.get("recording") or {}).get("state")
        if state == "complete":
            break
        if state == "incomplete":
            return {"status": "unavailable", "reason": "recording_incomplete"}
        if asyncio.get_running_loop().time() >= deadline:
            return {"status": "unavailable", "reason": "recording_finalization_unavailable"}
        await asyncio.sleep(min(0.05, max(0, deadline - asyncio.get_running_loop().time())))
    try:
        snapshot = recording_snapshot(service, current)
        pcm = recipient_pcm(snapshot)
        if not snapshot["metadata"].get("recipient_bytes"):
            raise AudioEvidenceUnavailable("recipient_audio_unavailable")
        source = {
            "recording_digest": snapshot["digest"],
            "recipient_pcm_sha256": hashlib.sha256(pcm).hexdigest(),
            "sample_rate_hz": 8000,
            "duration_ms": snapshot["metadata"]["duration_ms"],
            "boundary": "recipient_audio_received_from_carrier",
        }
        configured = [(directory, "small.en")]
        confirmation = getattr(
            service.settings, "post_call_audio_confirmation_model_directory", None
        )
        if confirmation is not None:
            configured.append((confirmation, "medium.en"))
        completed, stop = [], threading.Event()
        try:
            await asyncio.wait_for(
                asyncio.to_thread(_recognize_once, pcm, configured, source, completed, stop),
                timeout=RECOGNITION_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            stop.set()
            # Completed observations survive a slow confirmation. No third
            # source is preferred, reconciled or chosen by this relay.
            pass
        except asyncio.CancelledError:
            stop.set()
            raise
        completed = copy.deepcopy(completed)
        while len(completed) < len(configured):
            name = configured[len(completed)][1]
            completed.append(
                {
                    "status": "unavailable",
                    "reason": "local_recognition_timed_out",
                    "source": source,
                    "recognizer": {**RECOGNIZER, "model": name},
                }
            )
        primary = completed[0]
        if len(completed) > 1:
            primary["additional_observations"] = completed[1:]
        return primary
    except AudioEvidenceUnavailable as error:
        return {"status": "unavailable", "reason": str(error)}
    except Exception:
        return {"status": "unavailable", "reason": "local_recognition_failed"}
