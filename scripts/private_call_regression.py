"""Seal/check a completed call's PRIVATE evidence without network or model use.

Run: uv run python scripts/private_call_regression.py seal|check PRIVATE_DIRECTORY
The seal lives beside the evidence in the ignored directory, never in Git.
"""

import argparse
import hashlib
import json
import os
import stat
import wave
from pathlib import Path

FILES = (
    "prepared.json",
    "worker-final-result.json",
    "worker-start-attempt.json",
    "carrier-closure.json",
    "restoration.json",
    "outcome-audit.json",
    "evidence/conversation.wav",
    "evidence/transcript.json",
    "evidence/audio-transcript.json",
)


def private_file(directory, name):
    path = directory / name
    for part in [path, *path.parents]:
        if part == directory.parent:
            break
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_mode & 0o077:
            raise ValueError("private evidence permissions required")
    if not path.is_file():
        raise ValueError("evidence file required")
    return path


def verify(directory):
    for name in FILES:
        private_file(directory, name)

    def read(name):
        return json.loads((directory / name).read_text())

    prepared, result = read("prepared.json"), read("worker-final-result.json")
    for key in ("call_id", "revision", "brief_hash"):
        if result[key] != prepared[key]:
            raise ValueError("call scope mismatch")
    if result["state"] != "completed" or result["outcome"] != "completed":
        raise ValueError("completed call required")
    if (
        not result["usage"]["voice_closed_confirmed"]
        or not result["usage"]["realtime_usage_confirmed"]
    ):
        raise ValueError("voice closure and usage required")
    if result["recording"]["state"] != "complete" or any(
        result["recording"][key] <= 0 for key in ("assistant_bytes", "recipient_bytes")
    ):
        raise ValueError("complete two-sided recording required")
    transcript = read("evidence/transcript.json")
    if transcript != result["transcript"]:
        raise ValueError("transcript differs from result")
    recipient = " ".join(e["text"] for e in transcript if e["role"] == "user")
    if not result["confirmed_facts"] or any(
        f["quote"] not in recipient for f in result["confirmed_facts"]
    ):
        raise ValueError("observed recipient quotes required")
    carrier, restoration = read("carrier-closure.json"), read("restoration.json")
    if carrier["status"] != "completed" or carrier["active_carrier_calls"] != 0:
        raise ValueError("carrier closure required")
    if restoration["phone"]["DOTS_PHONE_DRY_RUN"] != "true" or not all(
        restoration[key] for key in ("route_unchanged", "all_eight_persistent_ids_preserved")
    ):
        raise ValueError("restoration required")
    with wave.open(str(directory / "evidence/conversation.wav")) as audio:
        if audio.getnchannels() != 2 or audio.getframerate() != 8000 or audio.getsampwidth() != 2:
            raise ValueError("expected stereo carrier recording")
        if abs(audio.getnframes() / 8 - result["recording"]["duration_ms"]) > 1:
            raise ValueError("recording duration mismatch")
    return {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in FILES}


def regression(directory, mode):
    hashes = verify(directory)
    lock = directory / "private-acceptance-lock.json"
    if mode == "seal":
        with os.fdopen(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as output:
            json.dump({"schema": 1, "sha256": hashes}, output, indent=2)
    else:
        saved = json.loads(private_file(directory, lock.name).read_text())
        if saved.get("schema") != 1 or saved["sha256"] != hashes:
            raise ValueError("private evidence changed since acceptance")
    return {"passed": True, "files_checked": len(hashes), "network_calls": 0, "model_calls": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["seal", "check"])
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    print(json.dumps(regression(args.directory.absolute(), args.mode)))
