"""Read-only local setup diagnostics. Never contact a phone or model provider."""

from __future__ import annotations

import ipaddress
import json
import re
import shutil
import subprocess
import time
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from dots_phone.audio_review_assets import verify_assets
from dots_phone.bws import MAPPING, read_private, validate_values
from dots_phone.config import live_number
from dots_phone.deepfilter_assets import verify_model
from dots_phone.skills import SkillCatalog

LIMITS = [
    "Remote registration eligibility and authenticated ChatGPT linking are not verified.",
    "Provider funding, paid model access, carrier audio and task completion are not verified.",
    "Connected-source retrieval and acoustic quality are not verified.",
]


def command(arguments):
    """Capture potentially private subprocess output; never forward it to the user."""
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=15, check=False)
    if result.returncode or len(result.stdout) > 4_000_000:
        raise ValueError("local_command_failed")
    return result.stdout


def private_json(path, limit=64000):
    path = Path(path)
    if path.lstat().st_size > limit:
        raise ValueError("private_file_too_large")
    return json.loads(read_private(path))


def public_https(value, *, origin=False):
    if not isinstance(value, str) or re.search(r"[\s\x00-\x1f\x7f]", value):
        return False
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return False
    if (
        parsed.scheme != "https"
        or not host
        or "." not in host
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or (origin and parsed.path not in {"", "/"})
        or host in {"example.com", "example.net", "example.org"}
        or host.endswith(
            (
                ".example",
                ".example.com",
                ".example.net",
                ".example.org",
                ".invalid",
                ".test",
                ".localhost",
                ".local",
            )
        )
        or any(part in host for part in ("your-owner", "your-media", "your-local"))
    ):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return True


def mounted_path(service, target, *, read_only=False):
    """Resolve a container path through an explicit bind mount, without guessing."""
    if not isinstance(target, str) or not target.startswith("/"):
        raise ValueError("absolute_container_path_required")
    wanted = PurePosixPath(target)
    if ".." in wanted.parts:
        raise ValueError("container_path_must_not_traverse")
    candidates = []
    for mount in service.get("volumes", []):
        if not isinstance(mount, dict) or mount.get("type") != "bind":
            continue
        base = PurePosixPath(mount.get("target", ""))
        if base == wanted or base in wanted.parents:
            candidates.append((len(base.parts), mount, base))
    if not candidates:
        raise ValueError("bind_mount_required")
    _, mount, base = max(candidates, key=lambda item: item[0])
    if read_only and mount.get("read_only") is not True:
        raise ValueError("read_only_mount_required")
    return Path(mount["source"].replace("$$", "$")) / wanted.relative_to(base)


def running_services(project):
    if not isinstance(project, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", project):
        raise ValueError("compose_project_required")
    ids = command(
        ["docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=" + project]
    ).split()
    if not ids:
        return {}
    if not all(re.fullmatch(r"[a-f0-9]{12,64}", value) for value in ids):
        raise ValueError("invalid_local_container_inventory")
    services = {}
    for item in json.loads(command(["docker", "inspect", *ids])):
        name = item.get("Config", {}).get("Labels", {}).get("com.docker.compose.service")
        if name in {"phone-app", "owner-ingress", "media-ingress", "tunnel-client"}:
            services[name] = item
    return services


def container_service(item):
    """Convert only the required inspected fields to Compose's normalized shape."""
    return {
        "command": item.get("Config", {}).get("Cmd", []),
        "secrets": [
            {"source": "tunnel-key"}
            for mount in item.get("Mounts", [])
            if mount.get("Destination") == "/run/secrets/tunnel-key"
        ],
    }


def configuration_matches(config, app, item, image):
    """Compare desired and effective state in memory; never return private differences."""
    actual_env = dict(value.split("=", 1) for value in item["Config"]["Env"] if "=" in value)
    for key, value in app.get("environment", {}).items():
        expected = str(value).lower() if isinstance(value, bool) else str(value or "")
        if actual_env.get(key) != expected.replace("$$", "$"):
            return False
    if item.get("Image") != image:
        return False
    expected_mounts = {}
    for mount in app.get("volumes", []):
        kind, source = mount["type"], mount["source"]
        if kind == "volume":
            volume = config.get("volumes", {}).get(source, {})
            source = volume.get(
                "name", source if volume.get("external") else config["name"] + "_" + source
            )
        elif kind == "bind":
            source = str(Path(source.replace("$$", "$")).resolve())
        else:
            continue
        expected_mounts[mount["target"]] = (kind, source, bool(mount.get("read_only")))
    for secret in app.get("secrets", []):
        source = config["secrets"][secret["source"]]["file"]
        target = secret.get("target", secret["source"])
        if not target.startswith("/"):
            target = "/run/secrets/" + target
        expected_mounts[target] = ("bind", str(Path(source.replace("$$", "$")).resolve()), True)
    actual_mounts = {}
    for mount in item.get("Mounts", []):
        kind = mount["Type"]
        if kind not in {"bind", "volume"}:
            continue
        source = mount["Name"] if kind == "volume" else str(Path(mount["Source"]).resolve())
        actual_mounts[mount["Destination"]] = (kind, source, not mount["RW"])
    return actual_mounts == expected_mounts


def check_setup(
    *, directory=Path(".secrets"), compose_files=(), env_file=None, profiles=(), running=False
):
    checks = []

    def add(name, condition, success, action):
        checks.append(
            {
                "id": name,
                "status": "ok" if condition else "incomplete",
                "message": success if condition else action,
            }
        )

    for name in ("git", "uv", "node", "codex", "docker"):
        add(
            "tool_" + name,
            bool(shutil.which(name)),
            name + " is available on PATH.",
            "Install " + name + " and make it available on PATH.",
        )
    try:
        command(["docker", "compose", "version", "--short"])
        compose_available = True
    except Exception:
        compose_available = False
    add(
        "compose",
        compose_available,
        "Docker Compose is available.",
        "Install or start Docker with its Compose plugin.",
    )
    try:
        result = subprocess.run(
            ["codex", "login", "status"], capture_output=True, text=True, timeout=15, check=False
        )
        signed_in = result.returncode == 0 and bool(
            re.search(r"logged in using chatgpt", result.stdout + result.stderr, re.I)
        )
    except Exception:
        signed_in = False
    add(
        "codex_sign_in",
        signed_in,
        "Codex reports ChatGPT sign-in.",
        "Run codex login and choose ChatGPT sign-in; API-key login does not support the worker.",
    )
    files = list(compose_files) or [Path("compose.local.yaml")]
    selected_env = env_file
    if not compose_files and selected_env is None:
        selected_env = Path(directory) / "config.env"
    arguments = ["docker", "compose"]
    if selected_env is not None:
        arguments += ["--env-file", str(selected_env)]
    else:
        # Frozen rendered files need no dotenv file; avoid reading a nearby .env.
        arguments += ["--env-file", "/dev/null"]
    for file in files:
        arguments += ["-f", str(file)]
    for profile in profiles or ("connection",):
        arguments += ["--profile", profile]
    try:
        config = json.loads(command([*arguments, "config", "--format", "json"]))
        app = config["services"]["phone-app"]
        env = app["environment"]
        if not isinstance(env, dict):
            raise ValueError("normalized_environment_required")
        env = {key: str(value) if value is not None else "" for key, value in env.items()}
    except Exception:
        add(
            "configuration",
            False,
            "",
            "Cannot render the selected Compose configuration. Use the same --env-file and --compose-file options as startup; inspect the error locally without sharing private values.",
        )
        return finish(checks, running)
    add("configuration", True, "Selected Compose configuration rendered privately.", "")
    add(
        "transcription_model",
        env.get("DOTS_PHONE_PIPECAT_TRANSCRIPTION_MODEL", "gpt-transcribe")
        in {"gpt-transcribe", "gpt-realtime-whisper"},
        "A supported committed-turn transcription model is selected; provider availability and acoustic accuracy are untested.",
        "Select gpt-transcribe or the explicit gpt-realtime-whisper rollback using DOTS_PHONE_PIPECAT_TRANSCRIPTION_MODEL, then recreate between calls.",
    )
    add(
        "caller_number",
        live_number(env.get("DOTS_PHONE_TWILIO_PHONE_NUMBER", "")),
        "Caller number has the supported format; ownership and carrier routing are untested.",
        "Set your owned voice-capable US/Canada caller number in the private configuration before enabling calls.",
    )
    try:
        runtime_file = config["secrets"]["runtime"]["file"].replace("$$", "$")
        runtime = private_json(runtime_file)
        if not isinstance(runtime, dict) or set(runtime) != set(MAPPING.values()):
            raise ValueError("runtime_contract_mismatch")
        if not all(isinstance(value, str) for value in runtime.values()):
            raise ValueError("runtime_values_must_be_strings")
        validate_values(runtime)
        credentials = True
    except Exception:
        credentials = False
    add(
        "credentials",
        credentials,
        "Owner-only runtime credentials have the expected format; validity and funding are untested.",
        "Check the selected runtime secret locally: it must be an owner-only regular JSON file with all expected credential names and valid formats. Preserve existing keys; do not rerun init over it.",
    )
    for key, label in (("PUBLIC_BASE_URL", "owner_origin"), ("MEDIA_BASE_URL", "media_origin")):
        add(
            label,
            public_https(env.get("DOTS_PHONE_" + key), origin=True),
            "HTTPS origin is configured; public reachability is untested.",
            "Set a real HTTPS "
            + ("owner" if key == "PUBLIC_BASE_URL" else "media")
            + " origin in the private configuration; placeholders and loopback origins cannot link the default connection.",
        )
    redirects = [
        value.strip()
        for value in env.get("DOTS_PHONE_OAUTH_REDIRECT_URIS", "").split(",")
        if value.strip()
    ]
    add(
        "oauth_callbacks",
        bool(redirects) and all(public_https(value) for value in redirects),
        "OAuth callback URLs are configured; exact registration matching is untested.",
        "Set the exact HTTPS callback URLs supplied by your own ChatGPT registration.",
    )
    add(
        "mcp_resource",
        public_https(env.get("DOTS_PHONE_MCP_RESOURCE_URL")),
        "MCP resource URL is configured; authenticated linking is untested.",
        "Set the exact HTTPS MCP resource URL from your own connection registration.",
    )
    live = {}
    if running:
        try:
            live = running_services(config.get("name"))
            inventory_ok = bool(live)
        except Exception:
            inventory_ok = False
        add(
            "running_inventory",
            inventory_ok,
            "Existing project containers were inspected read-only.",
            "Start the selected project, or allow read-only access to its Docker daemon, then repeat --running.",
        )
    tunnel = config.get("services", {}).get("tunnel-client")
    inspected_connection = tunnel is None
    inspected_tunnel = live.get("tunnel-client")
    if tunnel is None and inspected_tunnel is not None:
        tunnel = container_service(inspected_tunnel)
    try:
        commands = tunnel["command"]
        tunnel_id = next(
            value.split("=", 1)[1]
            for value in commands
            if value.startswith("--control-plane.tunnel-id=")
        )
        tunnel_ok = bool(tunnel_id.strip()) and not re.search(
            r"your|fixture|configure|\$\{", tunnel_id, re.I
        )
        if inspected_tunnel is not None and inspected_connection:
            key_file = next(
                m["Source"]
                for m in inspected_tunnel["Mounts"]
                if m["Destination"] == "/run/secrets/tunnel-key" and not m["RW"]
            )
        else:
            key_file = config["secrets"]["tunnel-key"]["file"].replace("$$", "$")
        tunnel_ok = tunnel_ok and len(read_private(key_file)) >= 20
        if running and inspected_tunnel is not None and not inspected_connection:
            current_id = next(
                value.split("=", 1)[1]
                for value in inspected_tunnel["Config"]["Cmd"]
                if value.startswith("--control-plane.tunnel-id=")
            )
            current_key = next(
                m
                for m in inspected_tunnel["Mounts"]
                if m["Destination"] == "/run/secrets/tunnel-key"
            )
            tunnel_ok = (
                tunnel_ok
                and current_id == tunnel_id
                and not current_key["RW"]
                and Path(current_key["Source"]).resolve() == Path(key_file).resolve()
            )
    except Exception:
        tunnel_ok = False
    add(
        "secure_tunnel",
        tunnel_ok,
        "Secure MCP Tunnel ID and owner-only key are present; remote access is untested.",
        "Include the connection Compose service, configure your tunnel ID and an owner-only tunnel key, then repeat. For a frozen deployment, --running can inspect its existing tunnel container.",
    )
    try:
        directory_path = mounted_path(
            app, env.get("DOTS_PHONE_LOCAL_REASONER_DIRECTORY", "/reasoner")
        )
        ready = private_json(directory_path / "reasoner-ready.json", 16000)
        age = time.time() * 1000 - ready["heartbeat_ms"]
        worker_ok = (
            str(env.get("DOTS_PHONE_LOCAL_REASONER_ENABLED", "false")).lower() == "true"
            and ready.get("schema") == 1
            and ready.get("serving") is True
            and 0 <= age < 5000
            and ready.get("prewarm_supported") is True
            and all(
                type(ready.get(name)) is int and ready.get(name) == 1
                for name in (
                    "decision_protocol",
                    "source_selection_protocol",
                    "post_call_review_protocol",
                )
            )
            and bool(re.fullmatch(r"[a-f0-9]{32}", ready.get("instance_id", "")))
        )
    except Exception:
        worker_ok = False
    add(
        "host_worker",
        worker_ok,
        "Host reasoner heartbeat and required protocols are current; no model turn ran.",
        "Start or restart the signed-in Codex worker with the same private directory mounted by Compose. Check directory permissions and use the current checkout.",
    )
    filter_mode = env.get("DOTS_PHONE_PIPECAT_INPUT_FILTER", "off")
    if filter_mode == "deepfilter":
        try:
            model = mounted_path(app, env.get("DOTS_PHONE_DEEPFILTER_MODEL_PATH"), read_only=True)
            verify_model(model)
            filter_ok = True
        except Exception:
            filter_ok = False
        add(
            "filter_assets",
            filter_ok,
            "DeepFilter weights match the pinned checksum and have a read-only mount.",
            "Install checksum-verified external weights and include the DeepFilter overlay with its read-only model mount. Keep the chosen filter setting until prerequisites are repaired or explicitly opt out.",
        )
    else:
        add(
            "filter_assets",
            filter_mode == "off",
            "Input filtering is explicitly off; native filter assets are not required.",
            "Select a supported input filter and its matching Compose configuration.",
        )
    audio_model = env.get("DOTS_PHONE_POST_CALL_AUDIO_MODEL_DIRECTORY", "")
    if audio_model:
        try:
            model_directory = mounted_path(app, audio_model, read_only=True)
            verify_assets(model_directory)
            audio_assets_ok = True
        except Exception:
            audio_assets_ok = False
        add(
            "audio_review_assets",
            audio_assets_ok,
            "Optional recipient-audio model files match the pinned checksums and have a read-only mount; recognition accuracy is untested.",
            "Verify the external small.en assets with python -m dots_phone.audio_review_assets --verify and include the audio-review overlay with its read-only model mount.",
        )
        add(
            "audio_review_recording",
            env.get("DOTS_PHONE_RETAIN_RECORDING", "true").lower() == "true",
            "Recording retention is enabled for optional local recipient-audio review.",
            "Optional recipient-audio review requires recording retention. Preserve an explicit recording opt-out; remove the optional overlay or enable retention only with the owner's agreement.",
        )
    else:
        checks.append(
            {
                "id": "audio_review_assets",
                "status": "not_checked",
                "message": "Optional local recipient-audio review is off. Transcript conflicts may remain unresolved; local review also does not guarantee an accurate result.",
            }
        )
    confirmation_model = env.get("DOTS_PHONE_POST_CALL_AUDIO_CONFIRMATION_MODEL_DIRECTORY", "")
    if confirmation_model:
        try:
            confirmation_directory = mounted_path(app, confirmation_model, read_only=True)
            verify_assets(confirmation_directory, model="medium.en")
            confirmation_ok = bool(audio_model)
        except Exception:
            confirmation_ok = False
        add(
            "audio_review_confirmation_assets",
            confirmation_ok,
            "Optional confirmation-model files match the pinned medium.en checksums and have a read-only mount; recognition accuracy is untested.",
            "Verify external medium.en assets with python -m dots_phone.audio_review_assets --model medium.en --verify and include its read-only mount alongside the primary small.en model.",
        )
    if running:
        tunnel_running = bool(
            inspected_tunnel and inspected_tunnel.get("State", {}).get("Running") is True
        )
        add(
            "tunnel_running",
            tunnel_running,
            "Secure MCP Tunnel container is running; authenticated remote linking is untested.",
            "Start or repair the selected Secure MCP Tunnel container. Configured tunnel credentials alone do not establish a running connection.",
        )
        try:
            image = command(["docker", "image", "inspect", app["image"], "--format", "{{.Id}}"])
            applied = configuration_matches(config, app, live["phone-app"], image.strip())
        except Exception:
            applied = False
        add(
            "configuration_applied",
            applied,
            "Running app image, environment and mounts match the selected configuration.",
            "The running app differs from the selected image, environment or mounts, or could not be inspected. Recreate it with the exact private configuration after active calls close, then repeat the checks.",
        )
        target_version = SkillCatalog().workflow()["version"]
        for service in ("phone-app", "owner-ingress", "media-ingress"):
            item = live.get(service, {})
            try:
                container = item["Id"]
                if (
                    not re.fullmatch(r"[a-f0-9]{12,64}", container)
                    or item["State"]["Running"] is not True
                ):
                    raise ValueError("running_container_required")
                probe = "import urllib.request; client=urllib.request.build_opener(urllib.request.ProxyHandler({})); print(client.open('http://127.0.0.1:8000/healthz',timeout=2).read().decode())"
                health = json.loads(command(["docker", "exec", container, "python", "-c", probe]))
                healthy = health.get("status") == "ok"
                if service != "phone-app":
                    healthy = healthy and health.get("mcp_exposed") is False
            except Exception:
                healthy = False
            add(
                service + "_health",
                healthy,
                "Local service health passed; no external route was used.",
                "Check the selected local service and gateway startup; inspect logs privately. Health does not establish phone or OAuth readiness.",
            )
        try:
            container = live["phone-app"]["Id"]
            version = command(
                [
                    "docker",
                    "exec",
                    container,
                    "python",
                    "-c",
                    "import importlib.metadata; print(importlib.metadata.version('dots-phone'))",
                ]
            ).strip()
            version_ok = version == target_version
        except Exception:
            version_ok = False
        add(
            "runtime_version",
            version_ok,
            "Running package version matches this checkout's bundled workflow.",
            "The running image is unavailable or differs from this checkout. Follow the upgrade procedure, preserving keys and configuration, then refresh the installed plugin snapshot.",
        )
        if filter_mode == "deepfilter":
            try:
                library = env.get(
                    "DOTS_PHONE_DEEPFILTER_LIBRARY_PATH", "/opt/deepfilter/lib/libdf.so"
                )
                container = live["phone-app"]["Id"]
                found = (
                    command(
                        [
                            "docker",
                            "exec",
                            container,
                            "python",
                            "-c",
                            "import sys; from pathlib import Path; print(Path(sys.argv[1]).is_file())",
                            library,
                        ]
                    ).strip()
                    == "True"
                )
            except Exception:
                found = False
            add(
                "filter_library",
                found,
                "Native filter library is present; streaming behavior requires the isolated native checks.",
                "Rebuild with the native DeepFilter image and run the isolated streaming checks before relying on filtering.",
            )
        if audio_model:
            try:
                available = (
                    command(
                        [
                            "docker",
                            "exec",
                            live["phone-app"]["Id"],
                            "python",
                            "-c",
                            "import importlib.util; print(importlib.util.find_spec('faster_whisper') is not None)",
                        ]
                    ).strip()
                    == "True"
                )
            except Exception:
                available = False
            add(
                "audio_review_runtime",
                available,
                "Optional local recognizer package is present; no model was loaded and no audio was processed.",
                "Build the phone runtime with its audio-review dependencies before enabling the optional model mount.",
            )
    else:
        checks.append(
            {
                "id": "running_checks",
                "status": "not_checked",
                "message": "Use --running after startup to check local application/gateway health, image version and native library presence.",
            }
        )
    return finish(checks, running)


def finish(checks, running):
    return {
        "status": "incomplete"
        if any(item["status"] == "incomplete" for item in checks)
        else "ready",
        "scope": "running_local_installation" if running else "local_configuration",
        "checks": checks,
        "not_verified": LIMITS,
        "provider_calls": 0,
        "model_turns": 0,
    }


def format_report(report):
    title = "Local checks ready" if report["status"] == "ready" else "Local setup incomplete"
    lines = [title + " (" + report["scope"].replace("_", " ") + ")."]
    for item in report["checks"]:
        lines.append("[" + item["status"].upper() + "] " + item["id"] + ": " + item["message"])
    lines += ["Not verified: " + value for value in report["not_verified"]]
    lines.append("No calls, model turns, source queries or configuration changes were performed.")
    return "\n".join(lines)
