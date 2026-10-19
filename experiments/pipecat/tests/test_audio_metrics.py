import wave

import numpy as np

from audio_metrics import AudioCapture, pcm_fields, timing_metrics, word_received_ms


def test_recorded_pcm_and_word_clock_preserve_sample_offsets(tmp_path):
    capture = AudioCapture(tmp_path)
    pcm = np.full(2400, 1000, dtype="<i2").tobytes()
    assert capture.append("output", pcm)["stream_start_ms"] == 0
    assert capture.append("output", pcm)["stream_start_ms"] == 100
    capture.close()
    with wave.open(str(tmp_path / "output.wav")) as recording:
        assert recording.getnframes() == 4800
        assert recording.getframerate() == 24000
    timeline = [{"kind": "output_audio", "stream_start_ms": 100, "duration_ms": 100, "at_ms": 650}]
    assert word_received_ms(timeline, 0.15) == 650
    assert word_received_ms(timeline, 0.3) is None
    assert pcm_fields(pcm)["speech"]


def test_quiet_metric_requires_a_continuous_300ms_gap():
    timeline = [
        {"kind": "turn_sent", "turn_index": 1, "at_ms": 100},
        {"kind": "input_audio", "speech": True, "at_ms": 200},
        {"kind": "output_audio", "speech": False, "at_ms": 200},
        {"kind": "output_audio", "speech": True, "at_ms": 300},
        {"kind": "output_audio", "speech": False, "at_ms": 400},
        {"kind": "output_audio", "speech": False, "at_ms": 700},
        {"kind": "input_audio", "speech": True, "at_ms": 900},
        {"kind": "turn_sent", "turn_index": 2, "at_ms": 1000},
        {"kind": "close_requested", "at_ms": 1200},
        {"kind": "closed", "at_ms": 1400},
    ]
    metrics = timing_metrics(timeline)
    assert metrics["interruption_to_300ms_quiet_ms"] == 200
    assert metrics["close_ack_ms"] == 200
