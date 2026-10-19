"""PCM evidence at the Live adapter boundary, without provider transcription."""

import wave
from pathlib import Path

import numpy as np


def pcm_fields(pcm):
    samples = np.frombuffer(pcm, dtype="<i2").astype(float)
    rms = float(np.sqrt(np.mean(samples * samples))) if len(samples) else 0
    return {
        "speech": rms > 100,
        "rms": round(rms, 2),
        "duration_ms": round(len(samples) / 24, 3),
    }


class AudioCapture:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.streams = {}
        self.samples = {"input": 0, "output": 0}

    def append(self, direction, pcm):
        if direction not in self.streams:
            stream = wave.open(str(self.directory / f"{direction}.wav"), "wb")
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(24000)
            self.streams[direction] = stream
        start_ms = self.samples[direction] / 24
        self.streams[direction].writeframesraw(pcm)
        self.samples[direction] += len(pcm) // 2
        return {"stream_start_ms": round(start_ms, 3)}

    def close(self):
        for stream in self.streams.values():
            stream.close()


def word_received_ms(timeline, start_seconds):
    position_ms = start_seconds * 1000
    for event in timeline:
        if event["kind"] != "output_audio" or "stream_start_ms" not in event:
            continue
        if (
            event["stream_start_ms"]
            <= position_ms
            < event["stream_start_ms"] + event["duration_ms"]
        ):
            return event["at_ms"]
    return None


def timing_metrics(timeline):
    turns = {e["turn_index"]: e["at_ms"] for e in timeline if e["kind"] == "turn_sent"}
    interrupt, hangup = turns.get(1), turns.get(2)
    inputs = [
        e
        for e in timeline
        if e["kind"] == "input_audio"
        and e.get("speech")
        and interrupt is not None
        and hangup is not None
        and interrupt <= e["at_ms"] < hangup
    ]
    if not inputs:
        return {"measurement_state": "missing_input_speech"}
    first_input = inputs[0]["at_ms"]
    last_input = inputs[-1]["at_ms"]
    output = [
        e
        for e in timeline
        if e["kind"] == "output_audio" and first_input <= e["at_ms"] <= last_input
    ]
    quiet_start = None
    quiet_confirmed = None
    for event in output:
        if event["speech"]:
            quiet_start = None
        elif quiet_start is None:
            quiet_start = event["at_ms"]
        elif event["at_ms"] - quiet_start >= 300:
            quiet_confirmed = quiet_start
            break
    close = next((e["at_ms"] for e in timeline if e["kind"] == "close_requested"), None)
    closed = next((e["at_ms"] for e in timeline if e["kind"] == "closed"), None)
    return {
        "measurement_state": "measured",
        "lookup_input_first_voiced_ms": first_input,
        "lookup_input_last_voiced_ms": last_input,
        "interruption_to_300ms_quiet_ms": round(quiet_confirmed - first_input, 2)
        if quiet_confirmed is not None
        else None,
        "close_ack_ms": round(closed - close, 2)
        if close is not None and closed is not None
        else None,
        "boundary": "Input PCM submitted / output PCM received at the Live adapter; excludes playback, microphone and carrier delay.",
    }
