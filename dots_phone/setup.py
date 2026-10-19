"""Create local installation configuration and a plugin for your own connection."""

from __future__ import annotations

import argparse
import getpass
import json
import re
import shutil
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet

from dots_phone.bws import MAPPING, private_write, validate_values
from dots_phone.config import Settings
from dots_phone.oauth import HASHER


def https_url(value, *, origin=False):
    if re.search(r"[\s\x00-\x1f\x7f]", value):
        raise ValueError("URL must not contain whitespace or control characters")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or (origin and parsed.path not in {"", "/"})
    ):
        raise ValueError("use an HTTPS origin" if origin else "use an HTTPS MCP endpoint")
    return value.rstrip("/")


def setup_destination(directory):
    directory = Path(directory).expanduser().resolve()
    if re.search(r"[\x00-\x1f\x7f]", str(directory)):
        raise ValueError("setup path must not contain control characters")
    if directory.exists():
        raise ValueError("setup directory already exists; choose a new private directory")
    return directory


def dotenv_literal(value):
    # Compose single quotes keep spaces, # and $ literal; escape embedded quotes.
    return "'" + str(value).replace("'", "\\'") + "'"


def initialize(directory, *, origin, password, credentials):
    """No provider calls; never rotate existing encryption/authentication material."""
    directory = setup_destination(directory)
    https_url(origin, origin=True)
    if len(password) < 20:
        raise ValueError("owner password must have at least 20 characters")
    if set(credentials) != set(MAPPING.values()) - {
        "DOTS_PHONE_DATA_ENCRYPTION_KEY",
        "DOTS_PHONE_OAUTH_OWNER_SECRET_HASH",
    }:
        raise ValueError("provider credential names do not match the runtime contract")
    runtime = validate_values(
        {
            **credentials,
            "DOTS_PHONE_DATA_ENCRYPTION_KEY": Fernet.generate_key().decode(),
            "DOTS_PHONE_OAUTH_OWNER_SECRET_HASH": HASHER.hash(password),
        }
    )
    # Validate the same settings used by the service, without enabling dialing.
    Settings(
        public_base_url=origin,
        encryption_key=runtime["DOTS_PHONE_DATA_ENCRYPTION_KEY"],
        owner_secret_hash=runtime["DOTS_PHONE_OAUTH_OWNER_SECRET_HASH"],
    )
    directory.mkdir(mode=0o700, parents=True)
    (directory / "reasoner").mkdir(mode=0o700)
    private_write(directory / "runtime.json", json.dumps(runtime))
    private_write(
        directory / "config.env",
        f"DOTS_PHONE_RUNTIME_FILE={dotenv_literal(directory / 'runtime.json')}\n"
        f"DOTS_PHONE_PUBLIC_BASE_URL={origin.rstrip('/')}\n"
        "DOTS_PHONE_DRY_RUN=true\nDOTS_PHONE_VOICE_BACKEND=pipecat\n"
        "DOTS_PHONE_PIPECAT_ENGINE=realtime\nDOTS_PHONE_ACCOUNT_TASKS=true\n"
        "DOTS_PHONE_LOCAL_REASONER_ENABLED=true\nDOTS_PHONE_LOCAL_REASONER_MODEL=gpt-6-sol\n"
        "DOTS_PHONE_LOCAL_REASONER_RESPONSE_POLICY=direct_with_codex\n"
        "DOTS_PHONE_LOCAL_REASONER_VOICE_EFFORT=medium\n"
        f"DOTS_PHONE_LOCAL_REASONER_HOST_DIRECTORY={dotenv_literal(directory / 'reasoner')}\n"
        "DOTS_PHONE_CALL_AUTHORIZATION_MODE=connected_owner\n"
        "DOTS_PHONE_DESTINATION_POLICY=task_scope\n"
        "DOTS_PHONE_PIPECAT_ACCOUNT_DTMF=true\n"
        "DOTS_PHONE_CONNECTED_CALL_BUDGET_USD=3\nDOTS_PHONE_CONNECTED_DAILY_BUDGET_USD=10\n"
        "DOTS_PHONE_ALLOWED_TEST_NUMBERS=\nDOTS_PHONE_TWILIO_PHONE_NUMBER=\n"
        "DOTS_PHONE_CALLBACK_HOSTS=\nDOTS_PHONE_OAUTH_REDIRECT_URIS=\n",
    )
    return directory


def package_plugin(directory, *, app_id=None, mcp_url=None):
    """Generate a separate installable package; never modify the shared template."""
    if bool(app_id) == bool(mcp_url):
        raise ValueError("provide exactly one registered app ID or HTTPS MCP URL")
    if app_id and not re.fullmatch(r"asdk_app_[a-zA-Z0-9]+", app_id):
        raise ValueError("use your registered asdk_app ID")
    if mcp_url:
        https_url(mcp_url)
    directory = Path(directory)
    if directory.exists():
        raise ValueError("package destination already exists")
    template = Path(__file__).parent / "_plugin"
    if not template.is_dir():
        template = Path(__file__).resolve().parents[1] / "plugins" / "dots-phone"
    directory.mkdir(mode=0o700, parents=True)
    target = directory / "plugins" / "dots-phone"
    shutil.copytree(template, target)
    manifest = json.loads((target / "plugin.json").read_text())
    if app_id:
        manifest["extensions"]["com.openai"]["apps"] = "./.app.json"
        private_write(
            target / ".app.json", json.dumps({"apps": {"dots-phone": {"id": app_id}}}, indent=2)
        )
    else:
        mcp = {
            "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
            "mcpServers": {"dots-phone": {"type": "streamable-http", "url": mcp_url}},
        }
        (target / "mcp.json").write_text(json.dumps(mcp, indent=2) + "\n")
    (target / "plugin.json").write_text(json.dumps(manifest, indent=2) + "\n")
    catalog = {
        "name": "dots-phone",
        "plugins": [
            {
                "name": "dots-phone",
                "source": {"source": "local", "path": "./plugins/dots-phone"},
                "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                "category": "Productivity",
            }
        ],
    }
    private_write(
        directory / ".agents" / "plugins" / "marketplace.json", json.dumps(catalog, indent=2)
    )
    return directory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser(
        "init", help="prompt for your credentials; write private runtime files"
    )
    init.add_argument("--directory", type=Path, default=Path(".secrets"))
    init.add_argument("--origin", required=True)
    package = commands.add_parser(
        "package", help="build an installable package for your connection"
    )
    package.add_argument("--directory", type=Path, required=True)
    connection = package.add_mutually_exclusive_group(required=True)
    connection.add_argument("--app-id")
    connection.add_argument("--mcp-url")
    args = parser.parse_args()
    try:
        if args.command == "init":
            setup_destination(args.directory)
            https_url(args.origin, origin=True)
            credentials = {
                key: getpass.getpass(key + " (hidden): ")
                for key in MAPPING.values()
                if key
                not in {"DOTS_PHONE_DATA_ENCRYPTION_KEY", "DOTS_PHONE_OAUTH_OWNER_SECRET_HASH"}
            }
            password = getpass.getpass("New owner password (20+ characters): ")
            if password != getpass.getpass("Repeat owner password: "):
                raise ValueError("passwords do not match")
            initialize(
                args.directory, origin=args.origin, password=password, credentials=credentials
            )
            print(
                "Private configuration saved. Dialing is disabled; complete config.env before starting the local service."
            )
        else:
            package_plugin(args.directory, app_id=args.app_id, mcp_url=args.mcp_url)
            print(
                "Plugin package saved. Add its directory as a local plugin marketplace, then connect your account."
            )
    except Exception:
        # Provider validation exceptions can include input values. Do not print them.
        parser.exit(
            2,
            "Setup refused or failed. Check inputs and choose a new destination; existing files were not overwritten.\n",
        )


if __name__ == "__main__":
    main()
