"""Export one owner's encrypted call evidence locally, without dialing or store locks.

Usage: python -m dots_phone.export_evidence CALL_ID PRIVATE_DIRECTORY
Run in the configured phone container, then copy the private directory out.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import wave
from pathlib import Path

from cryptography.fernet import Fernet

from dots_phone.call_evidence import render_recording
from dots_phone.config import Settings


def private_file(path, mode):
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), mode)


def write_private(path, text):
    with private_file(path, "w") as output:
        output.write(text)


def export(settings, call_id, directory):
    if not re.fullmatch(r"[0-9a-f]{32}", call_id):
        raise ValueError("invalid call identifier")
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    crypt = Fernet(settings.encryption_key.get_secret_value().encode())
    db = sqlite3.connect(f"file:{settings.database}?mode=ro", uri=True)
    try:
        db.execute("BEGIN")  # One snapshot: manifest, chunks and transcript agree.
        row = db.execute(
            "SELECT payload FROM objects WHERE kind='call' AND id=? AND owner=?",
            (call_id, settings.owner),
        ).fetchone()
        if not row:
            raise ValueError("call unavailable")
        call = json.loads(crypt.decrypt(row[0]))
        metadata = call.get("recording")
        if not metadata or metadata["state"] != "complete":
            raise ValueError("complete recording unavailable")
        chunks = [
            json.loads(crypt.decrypt(row[0]))
            for row in db.execute(
                "SELECT payload FROM objects WHERE kind='call_audio' AND id LIKE ? AND owner=? ORDER BY id",
                (call_id + ":%", settings.owner),
            )
        ]
        audio = render_recording(metadata, chunks)
        with private_file(directory / "conversation.wav", "wb") as output:
            with wave.open(output, "wb") as wav:
                wav.setnchannels(2)
                wav.setsampwidth(2)
                wav.setframerate(8000)
                wav.writeframes(audio)
        transcript = call.get("transcript", [])
        write_private(directory / "transcript.json", json.dumps(transcript, indent=2) + "\n")
        write_private(
            directory / "transcript.txt",
            "\n\n".join(f"{e['role']}: {e['text']}" for e in transcript) + "\n",
        )
        write_private(directory / "manifest.json", json.dumps(metadata, indent=2) + "\n")
        write_private(
            directory / "keypad-commands.json",
            json.dumps(list(call.get("private_keypad_commands", {}).values()), indent=2) + "\n",
        )
        return {"recording": metadata, "transcript_turns": len(transcript)}
    finally:
        db.close()


def main():
    runtime = Path("/run/secrets/runtime")
    if runtime.exists():
        os.environ.update(json.loads(runtime.read_text()))
    print(json.dumps(export(Settings.from_env(), sys.argv[1], Path(sys.argv[2]))))


if __name__ == "__main__":
    main()
