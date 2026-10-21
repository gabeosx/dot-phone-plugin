from __future__ import annotations

import json
import os
import subprocess
import time

import pytest

from dots_phone import setup_check
from dots_phone.setup import initialize


@pytest.fixture
def installation(tmp_path, monkeypatch):
    private = tmp_path / "private settings"
    initialize(
        private,
        origin="https://owner.demo-tunnel.net",
        password="Fictional-owner-password-123",
        credentials={
            "OPENAI_API_KEY": "sk-" + "x" * 24,
            "TWILIO_ACCOUNT_SID": "AC" + "0" * 32,
            "TWILIO_AUTH_TOKEN": "0" * 32,
            "TWILIO_API_KEY": "SK" + "0" * 32,
            "TWILIO_API_SECRET": "x" * 24,
        },
    )
    key = private / "tunnel-key"
    key.write_text("synthetic-local-tunnel-key")
    key.chmod(0o600)
    heartbeat = private / "reasoner/reasoner-ready.json"
    heartbeat.write_text(
        json.dumps(
            {
                "schema": 1,
                "serving": True,
                "heartbeat_ms": time.time() * 1000,
                "prewarm_supported": True,
                "decision_protocol": 1,
                "source_selection_protocol": 1,
                "post_call_review_protocol": 1,
                "instance_id": "a" * 32,
            }
        )
    )
    heartbeat.chmod(0o600)
    config = {
        "name": "synthetic-installation",
        "services": {
            "phone-app": {
                "image": "dots-phone-synthetic:current",
                "environment": {
                    "DOTS_PHONE_PUBLIC_BASE_URL": "https://owner.demo-tunnel.net",
                    "DOTS_PHONE_MEDIA_BASE_URL": "https://media.demo-tunnel.net",
                    "DOTS_PHONE_MCP_RESOURCE_URL": "https://api.openai.com/v1/tunnel/tunnel_sample",
                    "DOTS_PHONE_OAUTH_REDIRECT_URIS": "https://chatgpt.com/connector_platform/oauth_redirect",
                    "DOTS_PHONE_LOCAL_REASONER_ENABLED": "true",
                    "DOTS_PHONE_LOCAL_REASONER_DIRECTORY": "/reasoner",
                    "DOTS_PHONE_PIPECAT_INPUT_FILTER": "off",
                    # Format-only synthetic input; this diagnostic never dials.
                    "DOTS_PHONE_TWILIO_PHONE_NUMBER": "+1" + "202555" + "0200",
                },
                "volumes": [
                    {"type": "bind", "source": str(private / "reasoner"), "target": "/reasoner"}
                ],
                "secrets": [{"source": "runtime", "target": "runtime"}],
            },
            "tunnel-client": {"command": ["run", "--control-plane.tunnel-id=tunnel_sample"]},
        },
        "secrets": {
            "runtime": {"file": str(private / "runtime.json")},
            "tunnel-key": {"file": str(key)},
        },
    }
    calls = []

    def command(arguments):
        calls.append(arguments)
        if arguments[:3] == ["docker", "compose", "version"]:
            return "5.5.0"
        if arguments[-3:] == ["config", "--format", "json"]:
            return json.dumps(config)
        raise AssertionError("Unexpected local command")

    monkeypatch.setattr(setup_check, "command", command)
    monkeypatch.setattr(setup_check.shutil, "which", lambda name: "/synthetic/bin/" + name)
    monkeypatch.setattr(
        setup_check.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args, 0, "", "Logged in using ChatGPT\n"
        ),
    )
    return private, config, calls


def check_id(report, name):
    return next(item for item in report["checks"] if item["id"] == name)


def local_containers(private, config):
    app = config["services"]["phone-app"]
    containers = {
        name: {"Id": digit * 64, "State": {"Running": True}}
        for name, digit in [("phone-app", "1"), ("owner-ingress", "2"), ("media-ingress", "3")]
    }
    containers["phone-app"].update(
        {
            "Image": "sha256:" + "f" * 64,
            "Config": {"Env": [key + "=" + value for key, value in app["environment"].items()]},
            "Mounts": [
                {
                    "Type": "bind",
                    "Source": str(private / "reasoner"),
                    "Destination": "/reasoner",
                    "RW": True,
                },
                {
                    "Type": "bind",
                    "Source": str(private / "runtime.json"),
                    "Destination": "/run/secrets/runtime",
                    "RW": False,
                },
            ],
        }
    )
    containers["tunnel-client"] = {
        "Id": "4" * 64,
        "State": {"Running": True},
        "Config": {"Cmd": list(config["services"]["tunnel-client"]["command"])},
        "Mounts": [
            {
                "Source": str(private / "tunnel-key"),
                "Destination": "/run/secrets/tunnel-key",
                "RW": False,
            }
        ],
    }
    return containers


def running_command(original, *, version="0.3.2"):
    def command(args):
        if args[:3] == ["docker", "image", "inspect"]:
            return "sha256:" + "f" * 64
        if args[:2] == ["docker", "exec"]:
            if "importlib.metadata" in args[-1]:
                return version + "\n"
            return json.dumps({"status": "ok", "mcp_exposed": False})
        return original(args)

    return command


def test_complete_local_configuration_is_narrowly_ready_without_mutation(installation):
    private, _, calls = installation
    before = {path: path.read_bytes() for path in private.rglob("*") if path.is_file()}
    report = setup_check.check_setup(directory=private)
    assert report["status"] == "ready"
    assert report["scope"] == "local_configuration"
    assert report["provider_calls"] == report["model_turns"] == 0
    assert any("paid model access" in item for item in report["not_verified"])
    assert check_id(report, "running_checks")["status"] == "not_checked"
    assert {path: path.read_bytes() for path in before} == before
    compose = next(args for args in calls if args[-3:] == ["config", "--format", "json"])
    assert compose[compose.index("--env-file") + 1] == str(private / "config.env")
    assert ["--profile", "connection"] == compose[-5:-3]


@pytest.mark.parametrize(
    "model,expected",
    [
        ("gpt-transcribe", "ok"),
        ("gpt-realtime-whisper", "ok"),
        ("", "incomplete"),
        ("unsupported-model", "incomplete"),
    ],
)
def test_transcription_setting_is_explicitly_validated(installation, model, expected):
    private, config, _ = installation
    config["services"]["phone-app"]["environment"]["DOTS_PHONE_PIPECAT_TRANSCRIPTION_MODEL"] = model
    report = setup_check.check_setup(directory=private)
    assert check_id(report, "transcription_model")["status"] == expected


@pytest.mark.parametrize(
    ("setting", "value", "failed"),
    [
        ("DOTS_PHONE_PUBLIC_BASE_URL", "https://configure.invalid", "owner_origin"),
        ("DOTS_PHONE_PUBLIC_BASE_URL", "https://owner.example.com", "owner_origin"),
        ("DOTS_PHONE_MEDIA_BASE_URL", "https://127.0.0.1", "media_origin"),
        ("DOTS_PHONE_MEDIA_BASE_URL", "https://user:private@media.demo-tunnel.net", "media_origin"),
        ("DOTS_PHONE_PUBLIC_BASE_URL", "https://[malformed", "owner_origin"),
        ("DOTS_PHONE_OAUTH_REDIRECT_URIS", "", "oauth_callbacks"),
        ("DOTS_PHONE_MCP_RESOURCE_URL", "", "mcp_resource"),
    ],
)
def test_missing_or_placeholder_registration_has_an_actionable_failure(
    installation, setting, value, failed
):
    private, config, _ = installation
    config["services"]["phone-app"]["environment"][setting] = value
    report = setup_check.check_setup(directory=private)
    assert report["status"] == "incomplete"
    assert check_id(report, failed)["status"] == "incomplete"
    assert value not in json.dumps(report) if value else True


@pytest.mark.parametrize("failure", ["malformed", "public_permissions", "symlink"])
def test_credential_failures_never_echo_private_inputs(installation, failure):
    private, _, _ = installation
    runtime = private / "runtime.json"
    marker = "PRIVATE-VALUE-MUST-NOT-APPEAR"
    if failure == "malformed":
        runtime.write_text('{"OPENAI_API_KEY": "' + marker + '"}')
    elif failure == "public_permissions":
        runtime.chmod(0o644)
    else:
        target = private / "saved-runtime.json"
        runtime.rename(target)
        runtime.symlink_to(target)
    report = setup_check.check_setup(directory=private)
    assert check_id(report, "credentials")["status"] == "incomplete"
    output = json.dumps(report) + setup_check.format_report(report)
    assert marker not in output and str(private) not in output


@pytest.mark.parametrize(
    "override",
    [
        {"heartbeat_ms": 0},
        {"heartbeat_ms": time.time() * 1000 + 60000},
        {"serving": False},
        {"source_selection_protocol": 0},
        {"decision_protocol": True},
        {"post_call_review_protocol": 0},
        {"prewarm_supported": False},
    ],
)
def test_stale_or_incompatible_host_worker_cannot_be_ready(installation, override):
    private, _, _ = installation
    heartbeat = private / "reasoner/reasoner-ready.json"
    heartbeat.write_text(json.dumps({**json.loads(heartbeat.read_text()), **override}))
    report = setup_check.check_setup(directory=private)
    assert check_id(report, "host_worker")["status"] == "incomplete"


def test_missing_reasoner_mount_is_not_guessed_from_an_unrelated_directory(installation):
    private, config, _ = installation
    config["services"]["phone-app"]["environment"]["DOTS_PHONE_LOCAL_REASONER_DIRECTORY"] = (
        "/different/private/location"
    )
    assert (
        check_id(setup_check.check_setup(directory=private), "host_worker")["status"]
        == "incomplete"
    )


def test_filter_rejects_unverified_weights_and_writable_mounts(installation, monkeypatch):
    private, config, _ = installation
    app = config["services"]["phone-app"]
    app["environment"].update(
        {
            "DOTS_PHONE_PIPECAT_INPUT_FILTER": "deepfilter",
            "DOTS_PHONE_DEEPFILTER_MODEL_PATH": "/models/weights.tar.gz",
        }
    )
    model = private / "not-real-model.tar.gz"
    model.write_bytes(b"independently-authored-invalid-fixture")
    mount = {
        "type": "bind",
        "source": str(model),
        "target": "/models/weights.tar.gz",
        "read_only": True,
    }
    app["volumes"].append(mount)
    assert (
        check_id(setup_check.check_setup(directory=private), "filter_assets")["status"]
        == "incomplete"
    )
    monkeypatch.setattr(setup_check, "verify_model", lambda path: None)
    assert check_id(setup_check.check_setup(directory=private), "filter_assets")["status"] == "ok"
    mount["read_only"] = False
    assert (
        check_id(setup_check.check_setup(directory=private), "filter_assets")["status"]
        == "incomplete"
    )


def configure_audio_review(private, config):
    app = config["services"]["phone-app"]
    app["environment"]["DOTS_PHONE_POST_CALL_AUDIO_MODEL_DIRECTORY"] = "/models/small.en"
    model = private / "external-model"
    model.mkdir()
    mount = {"type": "bind", "source": str(model), "target": "/models/small.en", "read_only": True}
    app["volumes"].append(mount)
    return app, model, mount


def test_audio_review_is_optional_and_does_not_claim_numeric_accuracy(installation):
    private, _, _ = installation
    report = setup_check.check_setup(directory=private)
    item = check_id(report, "audio_review_assets")
    assert item["status"] == "not_checked"
    assert "unresolved" in item["message"]
    assert report["status"] == "ready"


def test_audio_review_rejects_missing_corrupt_or_writable_assets(installation, monkeypatch):
    private, config, _ = installation
    _, model, mount = configure_audio_review(private, config)
    assert (
        check_id(setup_check.check_setup(directory=private), "audio_review_assets")["status"]
        == "incomplete"
    )
    (model / "model.bin").write_bytes(b"independently-authored-invalid-weights")
    assert (
        check_id(setup_check.check_setup(directory=private), "audio_review_assets")["status"]
        == "incomplete"
    )
    monkeypatch.setattr(setup_check, "verify_assets", lambda path: {})
    assert (
        check_id(setup_check.check_setup(directory=private), "audio_review_assets")["status"]
        == "ok"
    )
    mount["read_only"] = False
    assert (
        check_id(setup_check.check_setup(directory=private), "audio_review_assets")["status"]
        == "incomplete"
    )


def test_audio_review_preserves_recording_opt_out(installation, monkeypatch):
    private, config, _ = installation
    app, _, _ = configure_audio_review(private, config)
    app["environment"]["DOTS_PHONE_RETAIN_RECORDING"] = "false"
    monkeypatch.setattr(setup_check, "verify_assets", lambda path: {})
    report = setup_check.check_setup(directory=private)
    assert check_id(report, "audio_review_recording")["status"] == "incomplete"
    assert app["environment"]["DOTS_PHONE_RETAIN_RECORDING"] == "false"


def test_audio_review_confirmation_model_is_not_silently_certified(installation, monkeypatch):
    private, config, _ = installation
    config["services"]["phone-app"]["environment"][
        "DOTS_PHONE_POST_CALL_AUDIO_CONFIRMATION_MODEL_DIRECTORY"
    ] = "/models/unverified"
    report = setup_check.check_setup(directory=private)
    assert check_id(report, "audio_review_confirmation_assets")["status"] == "incomplete"
    app, model, _ = configure_audio_review(private, config)
    confirmation = private / "confirmation-model"
    confirmation.mkdir()
    app["volumes"].append(
        {
            "type": "bind",
            "source": str(confirmation),
            "target": "/models/unverified",
            "read_only": True,
        }
    )
    verified = []

    def verify(path, *, model="small.en"):
        verified.append((path, model))
        return {}

    monkeypatch.setattr(setup_check, "verify_assets", verify)
    report = setup_check.check_setup(directory=private)
    assert check_id(report, "audio_review_confirmation_assets")["status"] == "ok"
    assert (model, "small.en") in verified
    assert (confirmation, "medium.en") in verified
    app["volumes"][-1]["read_only"] = False
    assert (
        check_id(setup_check.check_setup(directory=private), "audio_review_confirmation_assets")[
            "status"
        ]
        == "incomplete"
    )


def test_audio_review_runtime_package_is_required_when_enabled(installation, monkeypatch):
    private, config, _ = installation
    _, model, _ = configure_audio_review(private, config)
    containers = local_containers(private, config)
    containers["phone-app"]["Mounts"].append(
        {"Type": "bind", "Source": str(model), "Destination": "/models/small.en", "RW": False}
    )
    monkeypatch.setattr(setup_check, "verify_assets", lambda path: {})
    monkeypatch.setattr(setup_check, "running_services", lambda project: containers)
    original = running_command(setup_check.command)
    for available in (False, True):

        def command(args, available=available):
            if "importlib.util" in args[-1]:
                return str(available)
            return original(args)

        monkeypatch.setattr(setup_check, "command", command)
        report = setup_check.check_setup(directory=private, running=True)
        assert check_id(report, "audio_review_runtime")["status"] == (
            "ok" if available else "incomplete"
        )


def test_compose_failure_captures_diagnostics_without_leaking_paths_or_values(
    installation, monkeypatch
):
    private, _, _ = installation
    marker = "PRIVATE-ERROR-VALUE"

    def failed(arguments):
        raise RuntimeError(str(private) + marker)

    monkeypatch.setattr(setup_check, "command", failed)
    report = setup_check.check_setup(compose_files=[private / "frozen config.json"])
    assert report["status"] == "incomplete"
    assert check_id(report, "configuration")["status"] == "incomplete"
    output = json.dumps(report) + setup_check.format_report(report)
    assert str(private) not in output and marker not in output


def test_running_old_image_is_reported_even_with_healthy_http(installation, monkeypatch):
    private, config, _ = installation
    containers = local_containers(private, config)
    monkeypatch.setattr(setup_check, "running_services", lambda project: containers)
    monkeypatch.setattr(
        setup_check, "command", running_command(setup_check.command, version="0.2.13")
    )
    report = setup_check.check_setup(directory=private, running=True)
    assert check_id(report, "phone-app_health")["status"] == "ok"
    assert check_id(report, "runtime_version")["status"] == "incomplete"
    assert report["status"] == "incomplete"


@pytest.mark.parametrize("state", ["missing", "stopped"])
def test_running_check_refuses_an_absent_or_stopped_tunnel(installation, monkeypatch, state):
    private, config, _ = installation
    containers = local_containers(private, config)
    if state == "missing":
        containers.pop("tunnel-client")
    else:
        containers["tunnel-client"]["State"]["Running"] = False
    monkeypatch.setattr(setup_check, "running_services", lambda project: containers)
    monkeypatch.setattr(setup_check, "command", running_command(setup_check.command))
    report = setup_check.check_setup(directory=private, running=True)
    assert report["status"] == "incomplete"
    assert [item["id"] for item in report["checks"] if item["status"] == "incomplete"] == [
        "tunnel_running"
    ]


@pytest.mark.parametrize("difference", ["origin", "model_mount", "secret_mount", "image"])
def test_healthy_running_service_must_match_selected_configuration(
    installation, monkeypatch, difference
):
    private, config, _ = installation
    containers = local_containers(private, config)
    app = containers["phone-app"]
    if difference == "origin":
        app["Config"]["Env"] = [
            value
            if not value.startswith("DOTS_PHONE_PUBLIC_BASE_URL=")
            else "DOTS_PHONE_PUBLIC_BASE_URL=https://configure.invalid"
            for value in app["Config"]["Env"]
        ]
    elif difference == "model_mount":
        app["Mounts"].append(
            {
                "Type": "bind",
                "Source": str(private / "unselected model"),
                "Destination": "/models/unselected",
                "RW": False,
            }
        )
    elif difference == "secret_mount":
        app["Mounts"][1]["RW"] = True
    else:
        app["Image"] = "sha256:" + "e" * 64
    monkeypatch.setattr(setup_check, "running_services", lambda project: containers)
    monkeypatch.setattr(setup_check, "command", running_command(setup_check.command))
    report = setup_check.check_setup(directory=private, running=True)
    assert check_id(report, "phone-app_health")["status"] == "ok"
    assert check_id(report, "runtime_version")["status"] == "ok"
    assert check_id(report, "configuration_applied")["status"] == "incomplete"
    assert report["status"] == "incomplete"
    assert str(private) not in json.dumps(report)


def test_matching_healthy_running_configuration_has_explicit_scope_limits(
    installation, monkeypatch
):
    private, config, _ = installation
    monkeypatch.setattr(
        setup_check, "running_services", lambda project: local_containers(private, config)
    )
    monkeypatch.setattr(setup_check, "command", running_command(setup_check.command))
    report = setup_check.check_setup(directory=private, running=True)
    assert report["status"] == "ready"
    assert report["scope"] == "running_local_installation"
    assert any("authenticated ChatGPT linking" in value for value in report["not_verified"])


def test_frozen_configuration_uses_existing_tunnel_mount_without_dotenv_guessing(
    installation, monkeypatch
):
    private, config, calls = installation
    config["services"].pop("tunnel-client")
    monkeypatch.setattr(
        setup_check,
        "running_services",
        lambda project: {
            "tunnel-client": {
                "Config": {"Cmd": ["run", "--control-plane.tunnel-id=tunnel_sample"]},
                "Mounts": [
                    {
                        "Source": str(private / "tunnel-key"),
                        "Destination": "/run/secrets/tunnel-key",
                        "RW": False,
                    }
                ],
            }
        },
    )
    report = setup_check.check_setup(compose_files=[private / "frozen config.json"], running=True)
    assert check_id(report, "secure_tunnel")["status"] == "ok"
    rendered = next(args for args in calls if args[-3:] == ["config", "--format", "json"])
    assert rendered[rendered.index("--env-file") + 1] == "/dev/null"


def test_json_cli_exits_incomplete_without_prompting_or_mutating(installation, monkeypatch, capsys):
    from dots_phone.setup import main

    private, _, _ = installation
    os.remove(private / "reasoner/reasoner-ready.json")
    monkeypatch.setattr("sys.argv", ["setup", "check", "--directory", str(private), "--json"])
    monkeypatch.setattr("getpass.getpass", lambda *args: pytest.fail("diagnostic must not prompt"))
    with pytest.raises(SystemExit) as stopped:
        main()
    assert stopped.value.code == 1
    output = capsys.readouterr().out
    assert json.loads(output)["status"] == "incomplete"
    assert str(private) not in output
