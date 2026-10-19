"""Offline acoustic audit; provider captions are not an input to content checks."""

import argparse
import json
import re
import wave
from collections import defaultdict
from pathlib import Path


def normalize(text):
    return re.sub(r"[^a-z0-9 ]", "", text.casefold())


def comparison(text):
    text = normalize(text)
    return all(
        (
            bool(re.search(r"\bomni\b.{0,90}\bjune (2(?:nd)?|two|second)\b", text)),
            bool(
                re.search(
                    r"\b(clift|cliff)s?\b.{0,90}\bmay (29(?:th)?|twenty ninth|twenty nine)\b", text
                )
            ),
            "later" in text,
            bool(re.search(r"june (2(?:nd)?|two|second)\b", text)),
            bool(re.search(r"may (29(?:th)?|twenty ninth|twenty nine)\b", text)),
            bool(re.search(r"\b(four|4) days\b", text)),
        )
    )


def audit(directory, model):
    result = json.loads((directory / "result.json").read_text())
    timeline = result["timeline"]
    with wave.open(str(directory / "audio/output.wav")) as audio:
        pcm = audio.readframes(audio.getnframes())
    groups = defaultdict(list)
    for event in timeline:
        if event["kind"] == "output_audio":
            groups[event["response_id"]].append(event)
    response_dir = directory / "audited-responses"
    response_dir.mkdir(mode=0o700, exist_ok=True)
    heard = []
    for index, (identity, chunks) in enumerate(groups.items()):
        path = response_dir / f"{index}.wav"
        pieces, offsets, total = [], [], 0
        for event in chunks:
            start = round(event["stream_start_ms"] * 24) * 2
            size = round(event["duration_ms"] * 24) * 2
            pieces.append(pcm[start : start + size])
            offsets.append((total, total + size / 48000, event["at_ms"]))
            total += size / 48000
        with wave.open(str(path), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(24000)
            audio.writeframes(b"".join(pieces))
        path.chmod(0o600)
        segments, _ = model.transcribe(
            str(path),
            language="en",
            word_timestamps=True,
            vad_filter=False,
            condition_on_previous_text=False,
        )
        words, text = [], []
        for segment in segments:
            text.append(segment.text)
            for word in segment.words or []:
                arrival = next(
                    (at for start, end, at in offsets if start <= word.start < end), None
                )
                words.append({"word": word.word, "start": word.start, "received_ms": arrival})
        heard.append(
            {
                "response_id": identity,
                "text": " ".join(text),
                "words": words,
                "first_voiced_ms": next((e["at_ms"] for e in chunks if e["speech"]), None),
            }
        )
    full_path = directory / "complete-conversation.wav"
    full_segments = []
    if full_path.exists():
        full_segments, _ = model.transcribe(
            str(full_path),
            language="en",
            word_timestamps=True,
            vad_filter=False,
            condition_on_previous_text=False,
        )
    full = [
        {
            "text": s.text,
            "start": s.start,
            "end": s.end,
            "words": [{"word": w.word, "start": w.start, "end": w.end} for w in s.words or []],
        }
        for s in full_segments
    ]
    boundaries = [
        e
        for e in timeline
        if e["kind"] == "caller_boundary" and e["event_type"].endswith("speech_stopped")
    ]
    last_voice = []
    previous = 0
    for boundary in boundaries:
        events = [
            e
            for e in timeline
            if e["kind"] == "input_audio"
            and e["speech"]
            and previous < e["at_ms"] <= boundary["at_ms"]
        ]
        last_voice.append(max((e["at_ms"] for e in events), default=None))
        previous = boundary["at_ms"]
    delayed = (
        json.loads((directory / "session-scope.json").read_text())["injected_read_delay_ms"] > 0
    )
    source = json.loads((directory / "bridge/source-timing.json").read_text())
    read = next((t for t in source["trace"] if t["operation"] == "read"), None)
    epoch = result["call"]["usage"]["realtime_stage_clock_epoch_ms"]
    mail_answer = next((r for r in heard if comparison(r["text"])), None)
    acknowledgement = next(
        (
            r
            for r in heard
            if re.search(r"\b(checking|let me check|ill check|let me look)\b", normalize(r["text"]))
        ),
        None,
    )
    prepared = next(
        (
            r
            for r in heard
            if "lobby" in normalize(r["text"])
            and re.search(r"\b(ten|10)(?:\b|am\b)", normalize(r["text"]))
        ),
        None,
    )
    pending_reply = next(
        (
            r
            for r in heard
            if "indoors" in normalize(r["text"])
            and "lobby" in normalize(r["text"])
            and prepared
            and read
            and prepared["first_voiced_ms"] < r["first_voiced_ms"]
            and r["first_voiced_ms"] + epoch < read["published_ms"]
        ),
        None,
    )
    # Word onset for the first substantive property name, not the acknowledgement.
    useful_word = next(
        (w for w in (mail_answer or {}).get("words", []) if "omni" in w["word"].casefold()), None
    )
    end = last_voice[2] if len(last_voice) > 2 else None

    def difference(at, finish):
        return round(at - finish, 2) if at is not None and finish is not None else None

    while_pending = (
        pending_reply and read and pending_reply["first_voiced_ms"] + epoch < read["published_ms"]
    )
    checks = {
        "full_conversation_recorded": full_path.exists(),
        "prepared_source_answer_heard": prepared is not None,
        "truthful_lookup_acknowledgement_heard": acknowledgement is not None,
        "complete_sourced_comparison_heard": mail_answer is not None
        and comparison(" ".join(s["text"] for s in full)),
        "pending_lookup_remained_responsive": bool(while_pending) if delayed else None,
        "speech_interruption_observed": any(
            e["kind"] == "response_done" and e["status"] == "cancelled" for e in timeline
        ),
        "short_answer_after_interruption": any(
            re.search(r"\b(four|4) days\b", normalize(r["text"]))
            and len(normalize(r["text"]).split()) <= 16
            and not re.search(r"\b(june|may|unknown|cannot|cant|not)\b", normalize(r["text"]))
            for r in heard
        ),
        "confirmed_information_task": (result["call"].get("result") or {}).get("outcome")
        == "completed",
        "clean_recipient_hangup": result["recipient_stop_observed"]
        and result["call"]["state"] == "completed",
        "usage_and_socket_confirmed": result["voice_usage_confirmed"] and result["voice_closed"],
        "no_provider_errors": not result["errors"] and result["failure"] is None,
        "scenario_passed": result["scenario_passed"],
    }
    metrics = {
        "prepared_answer_ms": difference(
            (prepared or {}).get("first_voiced_ms"), last_voice[1] if len(last_voice) > 1 else None
        ),
        "acknowledgement_ms": difference((acknowledgement or {}).get("first_voiced_ms"), end),
        "useful_email_answer_ms": difference((useful_word or {}).get("received_ms"), end),
        "pending_question_answer_ms": difference(
            (pending_reply or {}).get("first_voiced_ms"),
            last_voice[3] if delayed and len(last_voice) > 3 else None,
        ),
        "source_connector_ms": [t["connector_ms"] for t in source["trace"]],
        "injected_delay_ms": [t["injected_delay_ms"] for t in source["trace"]],
        "measurement_boundary": "Last voiced input PCM submitted to the Realtime adapter / voiced output PCM received. Excludes carrier and playback; content independently recognized from saved audio.",
    }
    report = {
        "checks": checks,
        "passed": all(value is not False for value in checks.values()),
        "checks_not_exercised": [name for name, value in checks.items() if value is None],
        "metrics": metrics,
        "responses": heard,
        "recording_transcript": full,
        "provider_caption_used": False,
        "additional_text_api_calls": 0,
    }
    (directory / "acoustic-audit.json").write_text(json.dumps(report, indent=2))
    (directory / "acoustic-audit.json").chmod(0o600)
    return {
        "directory": str(directory),
        "passed": report["passed"],
        "checks": checks,
        "metrics": metrics,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directories", type=Path, nargs="+")
    args = parser.parse_args()
    from faster_whisper import WhisperModel

    model = WhisperModel(
        "small.en",
        device="cpu",
        compute_type="int8",
        cpu_threads=4,
        download_root="artifacts/pipecat/models",
        local_files_only=True,
    )
    for directory in args.directories:
        print(json.dumps(audit(directory, model)), flush=True)
