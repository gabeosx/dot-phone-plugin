"""Audit an owned synthetic call supervised by a Dot or its delegated worker."""

import argparse
import audioop
import hashlib
import json
import re
import statistics
import wave
from pathlib import Path

import numpy as np


def gmail_marker_answer_heard(answer, match_count):
    if not re.search(r"\bgmail\b", answer, re.I):
        return False
    if match_count == 0:
        return bool(
            re.search(r"\b(no|zero)\b[^.!?]{0,100}\b(matching|matches|messages)\b", answer, re.I)
        )
    if match_count != 1 or re.search(r"\b(no|zero|not|didn't)\b", answer, re.I):
        return False
    return bool(re.search(r"\bmatching\b[^.!?]{0,80}\b(message|found)\b", answer, re.I))


def answer_phase_words(root, audio, offset, forwarded, hangup, fallback):
    if not (root / "answer-audio-asr.json").exists():
        return fallback
    proof = json.loads((root / "answer-phase.json").read_text())
    if (
        proof["source_sha256"] != hashlib.sha256(audio.read_bytes()).hexdigest()
        or proof["clip_sha256"]
        != hashlib.sha256((root / "answer-phase.wav").read_bytes()).hexdigest()
        or proof["silence_removed"] is not False
        or abs(proof["start_seconds"] - max(0, forwarded - offset)) > 0.001
        or abs(proof["end_seconds"] - (hangup - offset)) > 0.001
    ):
        raise ValueError("answer-phase transcription does not match the received audio interval")
    segments = json.loads((root / "answer-audio-asr.json").read_text())
    return [w for s in segments for w in s["words"]]


def audit(root):
    root = root.resolve()
    controller = json.loads((root / "controller-terminal.json").read_text())
    sid, caller = next(
        iter(json.loads((root / "owned-bot-status.json").read_text())["calls"].items())
    )
    native = json.loads((root / "native-dot-evidence.json").read_text())
    answer_source = native.get("answer_source", "scoped_decision")
    if answer_source not in {
        "scoped_decision",
        "approved_brief",
        "gmail_marker_lookup",
        "local_gmail_marker_lookup",
    }:
        raise ValueError("unrecognized answer source")
    supervisor_kind = native.get("supervisor_kind", "dot")
    if supervisor_kind not in {"dot", "dot_subagent"}:
        raise ValueError("unrecognized authenticated supervisor kind")
    if supervisor_kind == "dot_subagent" and not native.get("subagent_identity"):
        raise ValueError("delegated supervision requires the observed subagent identity")
    carrier = json.loads((root / "carrier-terminal.json").read_text())
    energy = json.loads((root / (sid + "-energy.json")).read_text())
    offset = statistics.median(
        s["at_seconds"] - s["timestamp_ms"] / 1000 for s in energy if s["rms"] > 200
    )
    zero = min(0, offset)
    audio = root / (sid + "-received.wav")
    with wave.open(str(audio)) as source:
        assert (source.getframerate(), source.getnchannels(), source.getsampwidth()) == (8000, 1, 2)
        assistant = np.frombuffer(source.readframes(source.getnframes()), dtype="<i2").astype(float)
    tracks, sources = [(offset - zero, assistant)], []
    assets = root.parent / "inputs/assets"
    fixture_manifest = None
    if (root / "caller-assets.json").exists():
        fixture_manifest = json.loads((root / "caller-assets.json").read_text())
        assets = Path(fixture_manifest["directory"]).resolve()
        if not assets.is_relative_to(root.parent):
            raise ValueError("caller assets must be in the synthetic artifact tree")
    for turn in caller["turns"]:
        source = assets / (turn["name"] + ".wav")
        if (
            fixture_manifest
            and hashlib.sha256(source.read_bytes()).hexdigest()
            != fixture_manifest["sha256"][turn["name"]]
        ):
            raise ValueError("caller fixture changed after test preparation")
        with wave.open(str(source)) as stream:
            pcm, _ = audioop.ratecv(
                stream.readframes(stream.getnframes()), 2, 1, stream.getframerate(), 8000, None
            )
        pcm = audioop.ulaw2lin(audioop.lin2ulaw(pcm, 2), 2)
        first = next(
            i / 8000
            for i in range(0, len(pcm) // 2, 160)
            if audioop.rms(pcm[i * 2 : (i + 160) * 2], 2) > 200
        )
        placement = turn["first_voice_sent_at_seconds"] - first - zero
        samples = np.frombuffer(pcm, dtype="<i2").astype(float)
        tracks.append((placement, samples))
        sources.append(
            {
                "name": turn["name"],
                "source": str(source),
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "placement_seconds": placement,
            }
        )
    length = max(round(at * 8000) + len(samples) for at, samples in tracks)
    mix = np.zeros(length)
    for at, samples in tracks:
        position = round(at * 8000)
        mix[position : position + len(samples)] += 0.8 * samples
    clipped = int(np.count_nonzero(abs(mix) > 32767))
    output = root / "complete-conversation.wav"
    with wave.open(str(output), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(np.clip(mix, -32768, 32767).astype("<i2").tobytes())
    manifest = {
        "synthetic_only": True,
        "kind": "reconstructed_two_sided_conversation",
        "assistant_source": str(audio),
        "assistant_sha256": hashlib.sha256(audio.read_bytes()).hexdigest(),
        "caller_sources": sources,
        "clock_offset_seconds": offset,
        "duration_seconds": length / 8000,
        "clipped_samples": clipped,
        "timing_preserved": True,
        "silence_removed": False,
        "output": str(output),
        "output_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "limitation": "Exact prerecorded caller fixtures aligned to sent times, with received assistant audio. Outbound caller carrier delay is excluded.",
    }
    (root / "complete-conversation.json").write_text(json.dumps(manifest, indent=2) + "\n")
    segments = json.loads((root / "received-audio-asr.json").read_text())
    words = [w for s in segments for w in s["words"]]
    interruption = next(t for t in caller["turns"] if t["name"] == "interrupt")
    hangup = next(t for t in caller["turns"] if t["name"] == "hangup")
    last = interruption["last_voice_sent_at_seconds"]
    forwarded = caller.get("supervisor_forwarding_observed_at_seconds", last)
    phase_words = answer_phase_words(root, audio, offset, forwarded, hangup["at_seconds"], words)
    answer_words = [
        w for w in phase_words if forwarded <= w["start"] + offset < hangup["at_seconds"]
    ]
    answer = " ".join(w["text"] for w in answer_words)
    mentions = len(re.findall(r"(?:five|5)\s*p\s*\.?\s*m\.?", answer, re.I))
    reply = next(
        (s["at_seconds"] for s in energy if s["at_seconds"] >= forwarded and s["rms"] > 200), None
    )
    five = next(
        (
            w
            for w in answer_words
            if re.fullmatch(r"(?:five|5)(?:\s*p\.?m\.?)?", w["text"].strip(" .,!?"), re.I)
        ),
        None,
    )
    spoken_time = next(
        (
            s["at_seconds"]
            for s in energy
            if five
            and s["timestamp_ms"] / 1000 + s["duration_ms"] / 1000 > five["start"]
            and s["timestamp_ms"] / 1000 < five["end"]
            and s["rms"] > 200
        ),
        None,
    )
    text = " ".join(s["text"] for s in segments)
    assertions = {
        "ai_introduction": bool(re.search(r"\bAI\b", text)),
        "speech_at_interruption": caller["voice_at_interruption"],
        "five_pm_heard_once": mentions == 1,
        "caller_hangup_and_stream_stop": caller["state"] == "completed"
        and caller["stream_stopped"],
        "both_carrier_legs_completed": len(carrier) == 2
        and all(c["status"] == "completed" for c in carrier),
        "model_closed": controller["usage"].get("voice_closed_confirmed") is True,
        "realtime_usage_confirmed": controller["usage"].get("realtime_usage_confirmed") is True,
        "native_dot_terminal_event_processed": native["terminal_event_processed"],
    }
    if answer_source in {"gmail_marker_lookup", "local_gmail_marker_lookup"}:
        assertions.pop("five_pm_heard_once")
        assertions["gmail_match_status_heard"] = gmail_marker_answer_heard(
            answer, native["match_count"]
        )
        trace = native["trace"]
        gmail = [e for e in trace if e["phase"] == "gmail_search"]
        pending_times = [e["at_ms"] for e in trace if e["phase"] == "pending_observed"]
        assertions["one_fresh_gmail_lookup_after_request"] = (
            native["gmail_lookup_count"] == 1
            and len(gmail) == 1
            and len(pending_times) == 1
            and gmail[0]["started_ms"] >= pending_times[0]
            and gmail[0].get("match_count") == native["match_count"]
        )
        assertions["no_mailbox_content_disclosed"] = (
            native["disclosure"] == "synthetic_marker_match_status_only"
            and native["mailbox_ids_retained"] is False
        )
    observations = [
        json.loads(line)
        for line in (root / "controller-observations.jsonl").read_text().splitlines()
    ]
    if answer_source == "approved_brief":
        proof = json.loads((root / "approved-brief-evidence.json").read_text())
        assertions["approved_preloaded_answer"] = (
            proof["call_id"] == controller["call_id"]
            and proof["approval_bound_to_brief"] is True
            and proof["closing_fact_disclosed"] is True
            and proof["closing_fact_value"] == "Five PM"
        )
        assertions["no_in_call_decision"] = native["answer_count"] == 0 and not any(
            s.get("state") == "awaiting_decision" for s in observations
        )
        begin = end = None
    elif answer_source == "local_gmail_marker_lookup":
        local = controller["usage"]["local_gmail_worker"]
        assertions["actual_local_worker_answer_delivered_once"] = (
            native["answer_count"] == 0
            and native["start_count"] == 1
            and local["gmail_lookup_count"] == native["gmail_lookup_count"] == 1
            and local["request_id"] == native["request_id"]
            and local["match_count"] == native["match_count"]
            and local["plugin_finished_ms"]
            <= local["result_received_ms"]
            <= local["result_delivered_ms"]
        )
        begin = local["request_created_ms"] / 1000
        end = local["result_delivered_ms"] / 1000
    else:
        assertions[
            "actual_dot_answer_submitted_once"
            if supervisor_kind == "dot"
            else "actual_subagent_answer_submitted_once"
        ] = native["answer_submitted_once"]
        begin = next(
            s["observed_at_epoch"] for s in observations if s.get("state") == "awaiting_decision"
        )
        end = next(
            s["observed_at_epoch"]
            for s in observations
            if s.get("progress") == "Scoped answer forwarded; playback unconfirmed."
        )
    result = {
        "call_id": controller["call_id"],
        "supervisor_kind": supervisor_kind,
        "answer_source": answer_source,
        "subagent_identity": native.get("subagent_identity"),
        "baseline_pass": all(assertions.values()),
        "assertions": assertions,
        "decision_round_trip_seconds": round(end - begin, 3) if begin is not None else None,
        "first_reply_after_caller_seconds": round(reply - last, 3) if reply else None,
        "spoken_five_pm_after_caller_seconds": round(spoken_time - last, 3)
        if spoken_time
        else None,
        "ai_introduction_count": len(re.findall(r"\bAI assistant\b", text, re.I)),
        "internal_metadata_spoken": bool(
            re.search(r"\bsupervisor\b|detection|verified answer", text, re.I)
        ),
        "phone_session_state": controller["state"],
        "task_outcome": controller["result"]["outcome"],
        "termination_reason": controller["result"]["termination_reason"],
        "total_cost_confirmed": False,
        "complete_conversation": str(output),
        "reconstruction_limitation": manifest["limitation"],
    }
    if answer_source in {"gmail_marker_lookup", "local_gmail_marker_lookup"}:
        gmail = next(e for e in native["trace"] if e["phase"] == "gmail_search")
        result.update(
            gmail_plugin_seconds=round((gmail["ended_ms"] - gmail["started_ms"]) / 1000, 3),
            gmail_match_count=native["match_count"],
            gmail_lookup_count=native["gmail_lookup_count"],
        )
        if answer_source == "local_gmail_marker_lookup":
            result["local_result_delivery_seconds"] = round(
                (local["result_delivered_ms"] - local["plugin_finished_ms"]) / 1000, 3
            )
        else:
            submission = next(e for e in native["trace"] if e["phase"] == "submit")
            result["worker_submission_round_trip_seconds"] = round(
                (submission["ended_ms"] - submission["started_ms"]) / 1000, 3
            )
    (root / "summary.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    result = audit(parser.parse_args().directory)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["baseline_pass"] else 1)
