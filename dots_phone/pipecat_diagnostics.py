"""Bounded metadata for native Realtime interruptions; never speech contents."""

from __future__ import annotations

import hashlib
import math
import re
from copy import deepcopy

LIMIT = 128
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_STATUSES = {"in_progress", "completed", "cancelled", "incomplete", "failed"}
_REASONS = {"turn_detected", "client_cancelled", "max_output_tokens", "content_filter"}
_CLOSE_CODES = {
    1000,
    1001,
    1002,
    1003,
    1005,
    1006,
    1007,
    1008,
    1009,
    1010,
    1011,
    1012,
    1013,
    1014,
    1015,
}
_CONNECTION_SIDES = {
    "carrier_stop": "carrier",
    "carrier_disconnect": "carrier",
    "carrier_close_requested": "carrier",
    "carrier_transport_disconnect": "logical_carrier",
    "carrier_transport_close_requested": "logical_carrier",
    "model_receive_ended": "model",
    "model_close_requested": "model",
    "model_closed": "model",
    "pipeline_stop": "pipeline",
}


def _enum(value, allowed):
    return value if value is None or isinstance(value, str) and value in allowed else "unknown"


def _number(value, *, maximum=2**53 - 1):
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and 0 <= value <= maximum
        and math.isfinite(value)
    ):
        return value
    return None


def _identity(value):
    # Do not retain arbitrary text even when it has identifier syntax. Hashing
    # preserves stable correlation across lifecycle/audio events in one call.
    if isinstance(value, str) and _ID.fullmatch(value):
        return hashlib.sha256(value.encode("ascii")).hexdigest()[:24]
    return None


def accepted_input_configuration(session):
    """Read only explicitly acknowledged fields, not inferred SDK defaults."""
    audio = session.get("audio")
    inputs = audio.get("input") if isinstance(audio, dict) else None
    inputs = inputs if isinstance(inputs, dict) else session
    result = {}
    if "turn_detection" in inputs:
        detection = inputs["turn_detection"]
        if detection is None or detection is False:
            result["turn_detection"] = detection
        elif isinstance(detection, dict):
            safe = {}
            if "type" in detection:
                safe["type"] = _enum(detection["type"], {"server_vad", "semantic_vad"})
            for key in ("threshold", "prefix_padding_ms", "silence_duration_ms"):
                if key in detection:
                    safe[key] = _number(detection[key], maximum=1 if key == "threshold" else 60000)
            if "eagerness" in detection:
                safe["eagerness"] = _enum(detection["eagerness"], {"auto", "low", "medium", "high"})
            for key in ("interrupt_response", "create_response"):
                if key in detection:
                    value = detection[key]
                    safe[key] = value if value is None or isinstance(value, bool) else "unknown"
            result["turn_detection"] = safe
        else:
            result["turn_detection"] = "unknown"
    noise_key = "noise_reduction" if "noise_reduction" in inputs else "input_audio_noise_reduction"
    if noise_key in inputs:
        reduction = inputs[noise_key]
        result["noise_reduction"] = (
            None
            if reduction is None
            else {"type": _enum(reduction.get("type"), {"near_field", "far_field"})}
            if isinstance(reduction, dict)
            else "unknown"
        )
    transcription_key = (
        "transcription" if "transcription" in inputs else "input_audio_transcription"
    )
    if transcription_key in inputs:
        transcription = inputs[transcription_key]
        if transcription is None:
            result["transcription"] = None
        elif isinstance(transcription, dict):
            if "model" in transcription:
                result["transcription"] = {
                    "model": _enum(
                        transcription["model"], {"gpt-transcribe", "gpt-realtime-whisper"}
                    )
                }
        else:
            result["transcription"] = "unknown"
    return result


class NativeInterruptionDiagnostics:
    """A bounded timeline and counters, with no decisions or asynchronous work."""

    def __init__(self, clock_epoch_ms):
        self.clock_epoch_ms = clock_epoch_ms
        self.events = []
        self.responses = {}
        self.event_count = 0
        self.responses_dropped = 0
        self.native_turn_state = {}
        self.connection_events = []
        self.connection_event_count = 0
        self.first_connection_event = None

    def _response(self, response_id, session_count):
        identity = _identity(response_id)
        if identity is None:
            return None
        key = (session_count, identity)
        if key not in self.responses:
            self.responses[key] = {
                "response_id": identity,
                "session_count": session_count,
                "model_audio_bytes": 0,
                "forwarded_audio_bytes": 0,
                "audio_delta_events": 0,
            }
            if len(self.responses) > LIMIT:
                self.responses.pop(next(iter(self.responses)))
                self.responses_dropped += 1
        return self.responses[key]

    def observe(self, event, *, at_ms, session_count):
        kind = event.get("type")
        if kind not in {
            "input_audio_buffer.speech_started",
            "input_audio_buffer.speech_stopped",
            "input_audio_buffer.committed",
            "response.created",
            "response.done",
        }:
            return False
        record = {"type": kind, "at_ms": at_ms, "session_count": session_count}
        if kind.startswith("input_audio_buffer."):
            identity = _identity(event.get("item_id"))
            if identity is not None:
                record["item_id"] = identity
            for key in ("audio_start_ms", "audio_end_ms"):
                if key in event:
                    record[key] = _number(event[key])
            field = {
                "input_audio_buffer.speech_started": "last_started",
                "input_audio_buffer.speech_stopped": "last_stopped",
                "input_audio_buffer.committed": "last_committed",
            }[kind]
            self.native_turn_state[field] = dict(record)
            self.native_turn_state["last_event"] = dict(record)
        else:
            response = event.get("response")
            response = response if isinstance(response, dict) else {}
            counters = self._response(response.get("id"), session_count)
            metadata = {"status": _enum(response.get("status"), _STATUSES)}
            details = response.get("status_details")
            if isinstance(details, dict):
                metadata["status_details_type"] = _enum(details.get("type"), _STATUSES)
                metadata["reason"] = _enum(details.get("reason"), _REASONS)
            record.update(metadata)
            if counters is not None:
                counters.update(metadata)
                counters["created_at_ms" if kind == "response.created" else "done_at_ms"] = at_ms
                record.update(
                    response_id=counters["response_id"],
                    model_audio_bytes=counters["model_audio_bytes"],
                    forwarded_audio_bytes=counters["forwarded_audio_bytes"],
                )
        self.events = [*self.events[-(LIMIT - 1) :], record]
        self.event_count += 1
        return True

    def connection(self, kind, *, at_ms, session_count, code=None, input_state=None):
        """Observe a local boundary without asserting its physical initiator."""
        if kind not in _CONNECTION_SIDES:
            return False
        record = {
            "kind": kind,
            "side": _CONNECTION_SIDES[kind],
            "at_ms": _number(at_ms),
            "session_count": _number(session_count),
        }
        if isinstance(code, int) and not isinstance(code, bool) and code in _CLOSE_CODES:
            record["code"] = code
        # The service supplies only numeric counters, booleans and fixed enums.
        # Keep the whitelist here as well so future callers cannot add content.
        safe = {}
        for key in (
            "queued_frames",
            "queued_ms",
            "inflight_frame_ms",
            "inflight_resampled_bytes",
            "resampler_pending_output_samples",
            "resampler_pending_output_ms",
            "input_frames_received",
            "input_frames_dispatched",
            "input_ms_received",
            "input_ms_dispatched",
            "model_audio_packets_sent",
            "leading_silence_dropped_ms",
        ):
            if key in (input_state or {}):
                safe[key] = _number(input_state[key])
        for key in (
            "stop_requested",
            "closing",
            "disconnecting",
            "recovering",
            "input_dispatch_cancelled",
        ):
            if isinstance((input_state or {}).get(key), bool):
                safe[key] = input_state[key]
        for key, allowed in (
            ("dispatch_phase", {"idle", "waiting_ready", "resampling", "sending"}),
            ("receive_outcome", {"returned", "cancelled", "exception"}),
        ):
            if key in (input_state or {}):
                safe[key] = _enum(input_state[key], allowed)
        record["input_state"] = safe
        self.connection_events = [*self.connection_events[-(LIMIT - 1) :], record]
        self.connection_event_count += 1
        if self.first_connection_event is None:
            self.first_connection_event = record
        return True

    def audio(self, response_id, size, *, at_ms, session_count, forwarded=False):
        record = self._response(response_id, session_count)
        if record is None:
            return
        if forwarded:
            record["forwarded_audio_bytes"] += size
        else:
            record["model_audio_bytes"] += size
            record["audio_delta_events"] += 1
            record.setdefault("first_audio_at_ms", at_ms)
            record["last_audio_at_ms"] = at_ms

    def report(self):
        return {
            "clock_epoch_ms": self.clock_epoch_ms,
            "clock": "monotonic offsets from clock_epoch_ms; provider audio timestamps use their own clock",
            "boundary": "Model audio received/forwarded to pipeline; not carrier delivery or playback",
            "audio_format": "pcm_s16le/24000/mono",
            "identifier_format": "sha256 first 24 hex characters of validated provider identifiers",
            "events": [dict(event) for event in self.events],
            "events_dropped": self.event_count - len(self.events),
            "responses": [dict(response) for response in self.responses.values()],
            "response_records_dropped": self.responses_dropped,
            "native_turn_state": deepcopy(self.native_turn_state),
            "native_turn_boundary": "Last observed native start, stop and commit; no inferred turn completion",
            "connection_lifecycle": {
                "boundary": "Local observation order, not the physical remote initiator. Receive-loop end is not proof of socket close; logical_carrier is the logical transport boundary.",
                "input_boundary": "Queue and in-flight snapshots are local processing state. A pending send has unknown provider receipt; resampler delay is held output, not a flushed or discarded amount.",
                "events": deepcopy(self.connection_events),
                "events_dropped": self.connection_event_count - len(self.connection_events),
                "first_observed": deepcopy(self.first_connection_event),
                "last_observed": deepcopy(self.connection_events[-1])
                if self.connection_events
                else None,
            },
        }
