"""Project-scoped BWS reads; no shell evaluation or secret output."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import stat
import subprocess
from pathlib import Path
from uuid import UUID

from cryptography.fernet import Fernet

from dots_phone.oauth import HASHER


def project_id():
    value = os.environ.get("DOTS_PHONE_BWS_PROJECT_ID", "").strip()
    try:
        return str(UUID(value))
    except ValueError:
        raise RuntimeError("set DOTS_PHONE_BWS_PROJECT_ID to your own BWS project UUID") from None


MAPPING = {
    "DOTS_PHONE_OPENAI_API_KEY": "OPENAI_API_KEY",
    "DOTS_PHONE_TWILIO_ACCOUNT_SID": "TWILIO_ACCOUNT_SID",
    "DOTS_PHONE_TWILIO_AUTH_TOKEN": "TWILIO_AUTH_TOKEN",
    "DOTS_PHONE_TWILIO_API_KEY_SID": "TWILIO_API_KEY",
    "DOTS_PHONE_TWILIO_API_KEY_SECRET": "TWILIO_API_SECRET",
    "DOTS_PHONE_DATA_ENCRYPTION_KEY": "DOTS_PHONE_DATA_ENCRYPTION_KEY",
    "DOTS_PHONE_OAUTH_OWNER_SECRET_HASH": "DOTS_PHONE_OAUTH_OWNER_SECRET_HASH",
}


def read_private(path):
    path = Path(path)
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise RuntimeError("secret file must be owner-only, regular and not a symlink")
    return path.read_text().strip()


def bws_json(args, token):
    env = {"PATH": os.environ.get("PATH", ""), "BWS_ACCESS_TOKEN": token, "HOME": str(Path.home())}
    result = subprocess.run(
        ["bws", *args, "--output", "json", "--color", "no"],
        capture_output=True,
        env=env,
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError("BWS operation failed; check machine access and network")
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise RuntimeError("BWS returned invalid data") from None


def map_entries(entries, required=True, *, project=None):
    project = project or project_id()
    output = {}
    for name, target in MAPPING.items():
        matches = [x for x in entries if x.get("key") == name]
        if len(matches) > 1:
            raise RuntimeError("duplicate BWS secret name: " + name)
        if not matches:
            if required:
                raise RuntimeError("missing BWS entry: " + name)
            continue
        item = matches[0]
        if item.get("projectId") != project or not item.get("value"):
            raise RuntimeError("invalid project membership or empty secret: " + name)
        output[target] = item["value"]
    return validate_values(output)


def validate_values(output):
    """Validate runtime credential formats without contacting a provider."""
    if "DOTS_PHONE_DATA_ENCRYPTION_KEY" in output:
        Fernet(output["DOTS_PHONE_DATA_ENCRYPTION_KEY"].encode())
    checks = {
        "OPENAI_API_KEY": r"sk-[A-Za-z0-9_-]{20,}",
        "TWILIO_ACCOUNT_SID": r"AC[0-9a-fA-F]{32}",
        "TWILIO_API_KEY": r"SK[0-9a-fA-F]{32}",
        "TWILIO_AUTH_TOKEN": r"[0-9a-fA-F]{32}",
        "TWILIO_API_SECRET": r"[A-Za-z0-9]{20,}",
        "DOTS_PHONE_OAUTH_OWNER_SECRET_HASH": r"\$argon2id\$v=19\$m=65536,t=3,p=4\$[^$]+\$[^$]+",
    }
    for key, pattern in checks.items():
        if key in output and not re.fullmatch(pattern, output[key]):
            raise RuntimeError("invalid credential format: " + key)
    return output


def private_write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["preflight", "serve", "compose", "setup"])
    parser.add_argument("--token-file", default=".secrets/bws-access-token")
    parser.add_argument("--approved-save", action="store_true")
    args = parser.parse_args()
    try:
        token = (
            read_private(args.token_file)
            if Path(args.token_file).exists()
            else getpass.getpass("BWS machine-account token (hidden; never paste in chat): ")
        )
        project = project_id()
        entries = bws_json(["secret", "list", project], token)
        values = map_entries(entries, required=args.action != "setup")
        if args.action == "setup":
            if not args.approved_save:
                raise RuntimeError("saving new application secrets requires explicit approval")
            missing = [
                n
                for n in ("DOTS_PHONE_DATA_ENCRYPTION_KEY", "DOTS_PHONE_OAUTH_OWNER_SECRET_HASH")
                if n not in {x["key"] for x in entries}
            ]
            if not missing:
                print("Application secrets already exist; no rotation performed.")
                return
            try:
                from bitwarden_sdk import BitwardenClient
            except ImportError:
                print(
                    "Optional Bitwarden SDK unavailable. Review its vendor license in "
                    "docs/RUNBOOK.md#optional-bitwarden, then install with "
                    "uv sync --frozen --extra bws (also --extra pipecat if used). "
                    "Local-file setup does not need this SDK. No secret values were logged."
                )
                raise SystemExit(2) from None

            project = bws_json(["project", "get", project], token)

            client = BitwardenClient()
            login = client.auth().login_access_token(token)
            if not login.success:
                raise RuntimeError("BWS SDK authentication failed")
            for name in missing:
                if name.endswith("SECRET_HASH"):
                    password = getpass.getpass(
                        "New owner password (save in your password manager): "
                    )
                    if len(password) < 20 or password != getpass.getpass("Repeat owner password: "):
                        raise RuntimeError("password mismatch or fewer than 20 characters")
                    value = HASHER.hash(password)
                    del password
                else:
                    value = Fernet.generate_key().decode()  # 32 random bytes, URL-safe base64.
                result = client.secrets().create(
                    UUID(project["organizationId"]),
                    name,
                    value,
                    "Dots Phone application credential; rotate deliberately",
                    [UUID(project_id())],
                )
                if not result.success:
                    raise RuntimeError("saving application secret failed")
                print(name + ": saved; value suppressed")
            return
        del token
        if args.action == "preflight":
            print(json.dumps({name: "present" for name in MAPPING}, indent=2))
            return
        # Explicit allowlist: only ordinary DOTS_PHONE config and required SDK values.
        env = {
            k: v
            for k, v in os.environ.items()
            if k in {"PATH", "LANG", "TZ"} or (k.startswith("DOTS_PHONE_") and k not in MAPPING)
        }
        env.update(values)
        env.pop("BWS_ACCESS_TOKEN", None)
        if args.action == "serve":
            os.execvpe(
                "uvicorn",
                [
                    "uvicorn",
                    "dots_phone.server:create_app",
                    "--factory",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "8000",
                    "--no-access-log",
                ],
                env,
            )
        else:
            # Docker receives file-based secrets, never provider credentials in its
            # CLI environment or Compose interpolation.
            for key in values:
                env.pop(key, None)
            env["HOME"] = str(Path.home())
            for key in ("DOCKER_HOST", "DOCKER_CONTEXT"):
                if key in os.environ:
                    env[key] = os.environ[key]
            path = Path(".secrets/runtime.json")
            private_write(path, json.dumps(values))
            try:
                subprocess.run(
                    [
                        "docker",
                        "compose",
                        "-f",
                        "compose.yaml",
                        "-f",
                        "compose.production.yaml",
                        "up",
                        "--build",
                    ],
                    env=env,
                    check=True,
                )
            finally:
                path.unlink(missing_ok=True)
    except Exception:
        print("BWS operation blocked or failed. No secret values were logged.")
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
