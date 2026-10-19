from __future__ import annotations

import json
import subprocess
import sys
import tarfile
from io import BytesIO
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/public_release_audit.py"


def repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / ".gitignore").write_text("artifacts/\n.secrets/\n")
    (tmp_path / "dots_phone").mkdir()
    return tmp_path


def run(root, *args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--report", "artifacts/audit.json", *args],
        cwd=root,
        capture_output=True,
        text=True,
    )


def test_snapshot_includes_untracked_source_and_excludes_private_state(tmp_path):
    root = repo(tmp_path)
    (root / "dots_phone/new.py").write_text("VALUE = 1\n")
    (root / "LICENSE").write_text("MIT License\nCopyright fictional contributors\n")
    (root / "NOTICE").write_text("Retained third-party notices\n")
    (root / ".secrets").mkdir()
    (root / ".secrets/runtime.json").write_text('{"private": true}')
    (root / "docs/research").mkdir(parents=True)
    (root / "docs/research/private.md").write_text("Private research")
    result = run(root, "--export-dir", "artifacts/export")
    assert result.returncode == 0, result.stderr
    export = root / "artifacts/export"
    assert (export / "dots_phone/new.py").read_text() == "VALUE = 1\n"
    assert (export / "LICENSE").read_bytes() == (root / "LICENSE").read_bytes()
    assert (export / "NOTICE").read_bytes() == (root / "NOTICE").read_bytes()
    assert not (export / ".git").exists()
    assert not (export / ".secrets").exists()
    assert not (export / "docs/research").exists()


def test_setup_skill_is_exported_without_unrelated_agent_configuration(tmp_path):
    root = repo(tmp_path)
    skill = root / ".agents/skills/dots-phone-setup"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: dots-phone-setup\ndescription: Setup\n---\n")
    (root / ".agents/private-config.json").write_text('{"local": true}')
    personal = root / ".agents/skills/personal"
    personal.mkdir()
    (personal / "SKILL.md").write_text("Personal instructions")
    result = run(root, "--export-dir", "artifacts/export")
    assert result.returncode == 0, result.stderr
    export = root / "artifacts/export"
    assert (export / ".agents/skills/dots-phone-setup/SKILL.md").read_bytes() == (
        skill / "SKILL.md"
    ).read_bytes()
    assert not (export / ".agents/private-config.json").exists()
    assert not (export / ".agents/skills/personal").exists()


def test_untracked_credential_refuses_export_without_leaking_value(tmp_path):
    root = repo(tmp_path)
    private = "sk-proj-" + "A" * 40
    (root / "dots_phone/new.py").write_text(f'TOKEN = "{private}"\n')
    result = run(root, "--export-dir", "artifacts/export")
    assert result.returncode == 1
    assert not (root / "artifacts/export").exists()
    report = (root / "artifacts/audit.json").read_text()
    assert private not in result.stdout + result.stderr + report
    assert json.loads(report)["issues"][0]["location"] == "dots_phone/new.py"


def test_symlink_cannot_export_external_data(tmp_path):
    root = repo(tmp_path)
    external = tmp_path.parent / (tmp_path.name + "-external")
    external.write_text("external fixture")
    (root / "dots_phone/link.txt").symlink_to(external)
    result = run(root, "--export-dir", "artifacts/export")
    assert result.returncode == 1
    assert "symlink_not_exported" in (root / "artifacts/audit.json").read_text()


def test_distribution_rejects_private_members(tmp_path):
    root = repo(tmp_path)
    archive = root / "private.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        item = tarfile.TarInfo("package/.secrets/runtime.json")
        content = b"{}"
        item.size = len(content)
        tar.addfile(item, BytesIO(content))
    result = run(root, "--distribution", str(archive))
    assert result.returncode == 1
    report = json.loads((root / "artifacts/audit.json").read_text())
    assert report["distributions"][0]["issues"][0]["category"] == "private_distribution_path"


def test_private_denylist_path_and_contents_are_redacted(tmp_path):
    root = repo(tmp_path)
    private = "fictional-private-owner"
    (root / "dots_phone" / f"{private}.txt").write_text(private)
    (root / ".secrets").mkdir()
    denylist = root / ".secrets/denylist.json"
    denylist.write_text(json.dumps([private]))
    result = run(root, "--denylist", str(denylist))
    assert result.returncode == 1
    report = (root / "artifacts/audit.json").read_text()
    # No raw paths may escape through the manifest either.
    assert private not in result.stdout + result.stderr + report
    assert '"location": "[private path]"' in report


def test_existing_export_is_never_overwritten(tmp_path):
    root = repo(tmp_path)
    dest = root / "artifacts/export"
    dest.mkdir(parents=True)
    (dest / "keep.txt").write_text("existing output")
    result = run(root, "--export-dir", str(dest))
    assert result.returncode == 2
    assert (dest / "keep.txt").read_text() == "existing output"


def test_zip_distribution_rejects_symlinks(tmp_path):
    import stat
    import zipfile

    root = repo(tmp_path)
    archive = root / "link.whl"
    with zipfile.ZipFile(archive, "w") as package:
        entry = zipfile.ZipInfo("dots_phone/link")
        entry.create_system = 3
        entry.external_attr = (stat.S_IFLNK | 0o777) << 16
        package.writestr(entry, "../private-target")
    result = run(root, "--distribution", str(archive))
    assert result.returncode == 2
    assert "private-target" not in result.stdout + result.stderr


def test_history_scan_finds_removed_private_value(tmp_path):
    root = repo(tmp_path)

    def git(*args):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)

    git("config", "user.name", "Fictional contributor")
    git("config", "user.email", "contributor@example.invalid")
    private = "fictional-old-private-value"
    fixture = root / "dots_phone/old.py"
    fixture.write_text(private)
    git("add", ".")
    git("commit", "-qm", "Initial fixture")
    fixture.write_text("Generic fixture")
    git("commit", "-am", "Remove old value")
    (root / ".secrets").mkdir()
    denied = root / ".secrets/denylist.json"
    denied.write_text(json.dumps([private]))
    result = run(root, "--history", "--denylist", str(denied))
    assert result.returncode == 1
    report = (root / "artifacts/audit.json").read_text()
    assert private not in result.stdout + result.stderr + report
    assert json.loads(report)["history_objects"] > 0


def test_call_media_and_private_evidence_do_not_enter_export_or_distribution(tmp_path):
    root = repo(tmp_path)
    for name in (
        "tests/example.wav",
        "tests/recordings/notes.txt",
        "tests/transcripts/call.json",
        "tests/private-evidence/window.json",
        "tests/private-tests/replay.py",
        "tests/research/report.md",
    ):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fictional private fixture")
    result = run(root, "--export-dir", "artifacts/export")
    assert result.returncode == 0, result.stderr
    assert not (root / "artifacts/export/tests").exists()
    archive = root / "media.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        item = tarfile.TarInfo("package/tests/example.wav")
        content = b"synthetic media"
        item.size = len(content)
        tar.addfile(item, BytesIO(content))
    result = run(root, "--distribution", str(archive))
    assert result.returncode == 1
    report = json.loads((root / "artifacts/audit.json").read_text())
    assert report["distributions"][0]["issues"][0]["category"] == "private_distribution_path"
