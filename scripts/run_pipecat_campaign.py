"""Private host launcher: BWS stays outside the Pipecat image."""

import argparse
import ast
import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from dots_phone.bws import PROJECT, bws_json, map_entries, private_write, read_private


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--authorization-reference", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    directory = args.directory.resolve()
    if directory.parent != root / "artifacts/pipecat" or (directory / "reservation.json").exists():
        raise SystemExit("Use a fresh direct child of artifacts/pipecat; no attempt started")
    model_path = root / ".secrets/pipecat-model-key"
    approval_path = root / ".secrets/pipecat-live-approval.json"
    if model_path.exists() or approval_path.exists():
        raise SystemExit("Temporary credential path is occupied; no overwrite performed")
    module = ast.parse((root / "experiments/pipecat/run_live.py").read_text())
    scope = ast.literal_eval(
        next(
            node.value
            for node in module.body
            if isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "SCOPE" for t in node.targets)
        )
    )
    approval = {
        **scope,
        "max_runs": 5,
        "record_audio": True,
        "approval_reference": args.authorization_reference,
        "authorized_at": datetime.now(UTC).isoformat(),
        "source_sha256": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (root / "experiments/pipecat").glob("*.py")
        },
        "scenario_sha256": hashlib.sha256(
            (root / "artifacts/pipecat/inputs/baseline.yaml").read_bytes()
        ).hexdigest(),
    }
    created = []
    result = None
    try:
        entries = bws_json(
            ["secret", "list", PROJECT], read_private(root / ".secrets/bws-access-token")
        )
        values = map_entries(
            [e for e in entries if e.get("key") == "DOTS_PHONE_OPENAI_API_KEY"], required=False
        )
        key = values.get("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("Missing scoped model key")
        private_write(model_path, key)
        created.append(model_path)
        del entries, values, key
        private_write(approval_path, json.dumps(approval, indent=2))
        created.append(approval_path)
        env = {
            k: v
            for k, v in os.environ.items()
            if k in {"PATH", "HOME", "LANG", "TZ", "DOCKER_HOST", "DOCKER_CONTEXT"}
        }
        command = [
            "docker",
            "compose",
            "-f",
            "compose.yaml",
            "-f",
            "compose.live.yaml",
            "--profile",
            "live",
            "run",
            "--rm",
            "live-runner",
            "python",
            "run_campaign.py",
            "--key-file=/run/secrets/model-key",
            "--approval-file=/run/secrets/live-approval",
            "--scenario=/fixtures/baseline.yaml",
            f"--output-directory=/artifacts/{directory.name}",
        ]
        result = subprocess.run(command, cwd=root / "experiments/pipecat", env=env, check=False)
    finally:
        for path in reversed(created):
            path.unlink(missing_ok=True)
        print("Temporary model credentials removed.", flush=True)
    raise SystemExit(result.returncode if result else 2)


if __name__ == "__main__":
    main()
