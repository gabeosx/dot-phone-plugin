from __future__ import annotations

import builtins

import pytest
from cryptography.fernet import Fernet

from dots_phone import bws
from dots_phone.oauth import HASHER


@pytest.fixture
def without_bitwarden(monkeypatch):
    original_import = builtins.__import__
    attempts = []

    def unavailable(name, *args, **kwargs):
        if name == "bitwarden_sdk":
            attempts.append(name)
            raise ImportError("synthetic private import detail")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", unavailable)
    return attempts


def test_missing_setup_sdk_reports_install_extra_without_exposing_secrets(
    tmp_path, monkeypatch, capsys, without_bitwarden
):
    project = "00000000-0000-4000-8000-000000000001"
    token = "synthetic-machine-token-not-for-output"
    monkeypatch.setenv("DOTS_PHONE_BWS_PROJECT_ID", project)
    monkeypatch.setattr(
        "sys.argv",
        ["bws", "setup", "--approved-save", "--token-file", str(tmp_path / "absent")],
    )
    monkeypatch.setattr(bws.getpass, "getpass", lambda _: token)
    requests = []

    def fake_bws(args, supplied_token):
        assert supplied_token == token
        requests.append(args)
        return []

    monkeypatch.setattr(bws, "bws_json", fake_bws)
    with pytest.raises(SystemExit) as stopped:
        bws.main()
    assert stopped.value.code == 2
    assert requests == [["secret", "list", project]]
    assert without_bitwarden == ["bitwarden_sdk"]
    output = capsys.readouterr().out
    assert "uv sync --frozen --extra bws" in output
    assert "vendor license" in output
    assert "Local-file setup does not need this SDK" in output
    assert token not in output
    assert "synthetic private import detail" not in output
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("action", ["setup", "preflight"])
def test_existing_bws_credentials_work_without_optional_sdk(
    action, tmp_path, monkeypatch, capsys, without_bitwarden
):
    project = "00000000-0000-4000-8000-000000000001"
    token = "synthetic-machine-token-not-for-output"
    values = {
        "OPENAI_API_KEY": "sk-" + "x" * 24,
        "TWILIO_ACCOUNT_SID": "AC" + "0" * 32,
        "TWILIO_AUTH_TOKEN": "0" * 32,
        "TWILIO_API_KEY": "SK" + "0" * 32,
        "TWILIO_API_SECRET": "x" * 24,
        "DOTS_PHONE_DATA_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "DOTS_PHONE_OAUTH_OWNER_SECRET_HASH": HASHER.hash("synthetic-owner-password"),
    }
    entries = [
        {"key": name, "projectId": project, "value": values[target]}
        for name, target in bws.MAPPING.items()
    ]
    monkeypatch.setenv("DOTS_PHONE_BWS_PROJECT_ID", project)
    monkeypatch.setattr(
        "sys.argv",
        ["bws", action, "--approved-save", "--token-file", str(tmp_path / "absent")],
    )
    monkeypatch.setattr(bws.getpass, "getpass", lambda _: token)
    requests = []

    def fake_bws(args, supplied_token):
        assert supplied_token == token
        requests.append(args)
        return entries

    monkeypatch.setattr(bws, "bws_json", fake_bws)
    bws.main()
    assert requests == [["secret", "list", project]]
    assert without_bitwarden == []
    output = capsys.readouterr().out
    assert token not in output
    assert all(value not in output for value in values.values())
    assert "no rotation performed" in output if action == "setup" else "present" in output
    assert not list(tmp_path.iterdir())
