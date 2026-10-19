from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from dots_phone.bws import MAPPING, map_entries, project_id
from dots_phone.oauth import HASHER
from dots_phone.setup import initialize, package_plugin


def compose_configuration(tmp_path, overrides, *overlays):
    if shutil.which("docker") is None:
        pytest.skip("Compose config validation requires the Docker CLI")
    root = Path(__file__).resolve().parents[1]
    empty = tmp_path / "empty.env"
    empty.write_text("")
    command = [
        "docker",
        "compose",
        "--env-file",
        str(empty),
        "-f",
        str(root / "compose.local.yaml"),
    ]
    for overlay in overlays:
        command += ["-f", str(root / overlay)]
    return json.loads(
        subprocess.check_output(
            [*command, "config", "--format", "json"],
            env={"PATH": os.environ["PATH"], **overrides},
        )
    )


def test_local_compose_forwards_configured_runtime_values(tmp_path):
    values = {
        "DOTS_PHONE_TEST_STOP_ON_HUMAN": "true",
        "DOTS_PHONE_TEST_RESPONSE_LIMIT": "17",
        "DOTS_PHONE_TRACE_REALTIME_STAGES": "true",
        "DOTS_PHONE_DECISION_TIMEOUT_SECONDS": "42",
        "DOTS_PHONE_CALL_STATE_WAIT_SECONDS": "3",
        "DOTS_PHONE_RETENTION_SECONDS": "7200",
        "DOTS_PHONE_MAX_CONCURRENT_CALLS": "1",
        "DOTS_PHONE_RETAIN_RECORDING": "false",
        "DOTS_PHONE_RETAIN_TRANSCRIPT": "false",
        "DOTS_PHONE_POST_CALL_AUDIO_MODEL_DIRECTORY": "/models/primary",
        "DOTS_PHONE_POST_CALL_AUDIO_CONFIRMATION_MODEL_DIRECTORY": "/models/confirmation",
        "DOTS_PHONE_LOCAL_REASONER_DIRECTORY": "/delegates/phone",
        "DOTS_PHONE_LOCAL_REASONER_HOST_DIRECTORY": str(tmp_path / "private reasoner"),
        "DOTS_PHONE_LOCAL_REASONER_VOICE_EFFORT": "high",
        "DOTS_PHONE_LOCAL_REASONER_EFFORT": "low",
    }
    config = compose_configuration(tmp_path, values)["services"]["phone-app"]
    for key, expected in values.items():
        if key != "DOTS_PHONE_LOCAL_REASONER_HOST_DIRECTORY":
            assert config["environment"][key] == expected
    mount = next(v for v in config["volumes"] if v["target"] == "/delegates/phone")
    assert mount["source"] == values["DOTS_PHONE_LOCAL_REASONER_HOST_DIRECTORY"]


@pytest.mark.parametrize("backend,engine", [("tac", "live"), ("pipecat", "live")])
def test_local_compose_does_not_ignore_explicit_engine_selection(tmp_path, backend, engine):
    values = {"DOTS_PHONE_VOICE_BACKEND": backend, "DOTS_PHONE_PIPECAT_ENGINE": engine}
    config = compose_configuration(tmp_path, values)
    actual = config["services"]["phone-app"]["environment"]
    assert {k: actual[k] for k in values} == values


@pytest.mark.parametrize("filter_mode", ["off", "deepfilter"])
@pytest.mark.parametrize("ivr_last", [False, True])
def test_ivr_test_overlay_preserves_audio_filter_in_either_order(tmp_path, filter_mode, ivr_last):
    overlays = ["compose.deepfilter.yaml", "compose.ivr-test.yaml"]
    if not ivr_last:
        overlays.reverse()
    config = compose_configuration(
        tmp_path,
        {
            "DOTS_PHONE_PIPECAT_INPUT_FILTER": filter_mode,
            "DOTS_PHONE_DEEPFILTER_MODEL_FILE": str(tmp_path / "external-model.tar.gz"),
            "DOTS_PHONE_TEST_RESPONSE_LIMIT": "0",
        },
        *overlays,
    )
    actual = config["services"]["phone-app"]["environment"]
    assert actual["DOTS_PHONE_PIPECAT_INPUT_FILTER"] == filter_mode
    assert actual["DOTS_PHONE_TEST_STOP_ON_HUMAN"] == "true"
    assert actual["DOTS_PHONE_TEST_RESPONSE_LIMIT"] == "0"


@pytest.fixture
def credentials():
    return {
        "OPENAI_API_KEY": "sk-" + "x" * 24,
        "TWILIO_ACCOUNT_SID": "AC" + "0" * 32,
        "TWILIO_AUTH_TOKEN": "0" * 32,
        "TWILIO_API_KEY": "SK" + "0" * 32,
        "TWILIO_API_SECRET": "x" * 24,
    }


def test_local_setup_produces_private_unique_runtime_without_bws(
    tmp_path, credentials, monkeypatch
):
    monkeypatch.delenv("DOTS_PHONE_BWS_PROJECT_ID", raising=False)
    keys = []
    for suffix in ("one", "two"):
        target = tmp_path / suffix
        initialize(
            target,
            origin="https://owner.example",
            password="local-owner-password-123",
            credentials=credentials,
        )
        data = json.loads((target / "runtime.json").read_text())
        assert set(data) == set(MAPPING.values())
        assert HASHER.verify(data["DOTS_PHONE_OAUTH_OWNER_SECRET_HASH"], "local-owner-password-123")
        Fernet(data["DOTS_PHONE_DATA_ENCRYPTION_KEY"].encode())
        keys.append(data["DOTS_PHONE_DATA_ENCRYPTION_KEY"])
        assert stat.S_IMODE(target.stat().st_mode) == 0o700
        for name in ("runtime.json", "config.env"):
            assert stat.S_IMODE((target / name).stat().st_mode) == 0o600
        config = (target / "config.env").read_text()
        assert "DOTS_PHONE_DRY_RUN=true" in config
        assert "DOTS_PHONE_CALL_AUTHORIZATION_MODE=connected_owner" in config
        assert "DOTS_PHONE_PIPECAT_ACCOUNT_DTMF=true" in config
        assert "DOTS_PHONE_DESTINATION_POLICY=task_scope" in config
        assert all(value not in config for value in credentials.values())
    assert len(set(keys)) == 2


def test_setup_never_overwrites_keys(tmp_path, credentials):
    target = tmp_path / "installation"
    initialize(
        target,
        origin="https://owner.example",
        password="local-owner-password-123",
        credentials=credentials,
    )
    original = (target / "runtime.json").read_bytes()
    with pytest.raises(ValueError, match="already exists"):
        initialize(
            target,
            origin="https://owner.example",
            password="local-owner-password-456",
            credentials=credentials,
        )
    assert (target / "runtime.json").read_bytes() == original


@pytest.mark.parametrize(
    "origin",
    [
        "http://owner.example",
        "https://owner.example\nDOTS_PHONE_DRY_RUN=false",
        "https://user:pass@owner.example",
        "https://owner.example/path",
        "https://owner.example/?x=y\nINJECTED=value",
    ],
)
def test_setup_rejects_unsafe_origin_before_writing(tmp_path, credentials, origin):
    target = tmp_path / "installation"
    with pytest.raises(ValueError):
        initialize(
            target, origin=origin, password="local-owner-password-123", credentials=credentials
        )
    assert not target.exists()


def test_bws_is_explicit_and_enforces_installer_project(monkeypatch):
    monkeypatch.delenv("DOTS_PHONE_BWS_PROJECT_ID", raising=False)
    with pytest.raises(RuntimeError, match="own BWS"):
        project_id()
    project = "00000000-0000-4000-8000-000000000001"
    monkeypatch.setenv("DOTS_PHONE_BWS_PROJECT_ID", project)
    assert project_id() == project
    with pytest.raises(RuntimeError, match="membership"):
        map_entries(
            [{"key": "DOTS_PHONE_OPENAI_API_KEY", "projectId": "different", "value": "not-read"}],
            required=False,
        )


@pytest.mark.parametrize("mode", ["registered", "http"])
def test_generated_package_is_separate_and_usable(tmp_path, mode):
    target = tmp_path / "install"
    options = (
        {"app_id": "asdk_app_fixture"}
        if mode == "registered"
        else {"mcp_url": "https://local-tunnel.example/mcp"}
    )
    package_plugin(target, **options)
    plugin = target / "plugins" / "dots-phone"
    manifest = json.loads((plugin / "plugin.json").read_text())
    assert manifest["license"] == "MIT"
    assert (plugin / "LICENSE").read_bytes() == (
        Path(__file__).resolve().parents[1] / "LICENSE"
    ).read_bytes()
    assert (plugin / "skills/dots-phone/references/calling.md").is_file()
    catalog = json.loads((target / ".agents/plugins/marketplace.json").read_text())
    assert (target / catalog["plugins"][0]["source"]["path"]).resolve() == plugin
    if mode == "registered":
        assert manifest["extensions"]["com.openai"]["apps"] == "./.app.json"
        assert (
            json.loads((plugin / ".app.json").read_text())["apps"]["dots-phone"]["id"]
            == "asdk_app_fixture"
        )
    else:
        assert "apps" not in manifest["extensions"]["com.openai"]
        assert not (plugin / ".app.json").exists()
        assert json.loads((plugin / "mcp.json").read_text())["mcpServers"]["dots-phone"] == {
            "type": "streamable-http",
            "url": "https://local-tunnel.example/mcp",
        }
    original = (plugin / "plugin.json").read_bytes()
    with pytest.raises(ValueError, match="already exists"):
        package_plugin(target, **options)
    assert (plugin / "plugin.json").read_bytes() == original


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"app_id": "bad"},
        {"mcp_url": "http://unsafe.example/mcp"},
        {"app_id": "asdk_app_fixture", "mcp_url": "https://local.example/mcp"},
    ],
)
def test_invalid_connection_cannot_create_package(tmp_path, options):
    target = tmp_path / "install"
    with pytest.raises(ValueError):
        package_plugin(target, **options)
    assert not target.exists()


def test_custom_setup_config_selects_its_own_compose_secret(tmp_path, credentials):
    if shutil.which("docker") is None:
        pytest.skip("Compose config validation requires the Docker CLI, not a running daemon")
    target = tmp_path / "private ' $HOME # settings"
    initialize(
        target,
        origin="https://owner.example",
        password="local-owner-password-123",
        credentials=credentials,
    )
    root = Path(__file__).resolve().parents[1]
    config = json.loads(
        subprocess.check_output(
            [
                "docker",
                "compose",
                "--env-file",
                str(target / "config.env"),
                "-f",
                str(root / "compose.local.yaml"),
                "config",
                "--format",
                "json",
            ],
            env={"PATH": os.environ["PATH"]},
        )
    )
    # Compose's reusable config output doubles literal dollars. Its actual
    # interpolation environment must retain the installer's path verbatim.
    rendered = config["secrets"]["runtime"]["file"].replace("$$", "$")
    assert Path(rendered) == target.resolve() / "runtime.json"
    environment = subprocess.check_output(
        [
            "docker",
            "compose",
            "--env-file",
            str(target / "config.env"),
            "-f",
            str(root / "compose.local.yaml"),
            "config",
            "--environment",
        ],
        env={"PATH": os.environ["PATH"]},
        text=True,
    )
    selected = next(
        row.partition("=")[2]
        for row in environment.splitlines()
        if row.startswith("DOTS_PHONE_RUNTIME_FILE=")
    )
    assert Path(selected) == target.resolve() / "runtime.json"
    assert config["services"]["phone-app"]["environment"]["DOTS_PHONE_DRY_RUN"] == "true"


def test_setup_refuses_existing_destination_before_asking_for_secrets(
    tmp_path, monkeypatch, capsys
):
    from dots_phone.setup import main

    monkeypatch.setattr(
        "sys.argv",
        ["setup", "init", "--directory", str(tmp_path), "--origin", "https://owner.example"],
    )

    def unexpected_prompt(*args):
        pytest.fail("existing installation must be detected before credential prompts")

    monkeypatch.setattr("getpass.getpass", unexpected_prompt)
    with pytest.raises(SystemExit) as stopped:
        main()
    assert stopped.value.code == 2
    assert "existing files were not overwritten" in capsys.readouterr().err


def test_setup_rejects_a_multiline_private_path(tmp_path, credentials):
    target = tmp_path / "private\nINJECTED=value"
    with pytest.raises(ValueError, match="control"):
        initialize(
            target,
            origin="https://owner.example",
            password="local-owner-password-123",
            credentials=credentials,
        )
    assert not target.exists()
