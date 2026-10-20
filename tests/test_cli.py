from pathlib import Path

from platformctl.cli import main


def test_diff_does_not_claim_external_version_drift(tmp_path: Path, capsys):
    install = tmp_path / "external"
    install.mkdir()
    manifest = tmp_path / "stack.yaml"
    manifest.write_text(
        f"schema: 1\nservices:\n  legacy:\n"
        f"    version: 3.1.0\n    install_path: {install}\n",
        encoding="utf-8",
    )
    assert main(["diff", "-f", str(manifest)]) == 0
    output = capsys.readouterr().out
    assert "version cannot be verified" in output
    assert "No managed version or systemd unit changes" in output
    assert "-> 3.1.0" not in output


def test_list_releases_external_rejected(tmp_path: Path, capsys):
    install = tmp_path / "external"
    install.mkdir()
    manifest = tmp_path / "stack.yaml"
    manifest.write_text(
        f"schema: 1\nservices:\n  legacy:\n"
        f"    version: 3.1.0\n    install_path: {install}\n",
        encoding="utf-8",
    )
    assert main(["list-releases", "legacy", "-f", str(manifest)]) == 2
    assert "list-releases is unavailable" in capsys.readouterr().err


def test_list_releases_only_direct_version_directories(tmp_path: Path, capsys):
    root = tmp_path / "softA"
    (root / "2.5.4-build178").mkdir(parents=True)
    (root / "2.5.5-build184").mkdir()
    (root / ".staging" / "old").mkdir(parents=True)
    (root / "current").symlink_to(root / "2.5.5-build184")
    (root / "previous").symlink_to(root / "2.5.4-build178")
    p = tmp_path / "stack.yaml"
    p.write_text(
        "schema: 1\nservices:\n  softA:\n"
        f"    version: 2.5.5-build184\n    root: {root}\n",
        encoding="utf-8",
    )
    assert main(["list-releases", "softA", "-f", str(p)]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "2.5.4-build178 (previous)",
        "2.5.5-build184 (current)",
    ]


def test_diff_shows_only_service_file_change(tmp_path: Path, capsys, monkeypatch):
    from platformctl.engine import Engine
    from platformctl.unit_files import UnitFileManager

    root = tmp_path / "controller"
    (root / "1.0.0").mkdir(parents=True)
    (root / "current").symlink_to(root / "1.0.0")
    source = tmp_path / "controller.service"
    source.write_text("[Service]\nExecStart=/opt/controller/current/run.sh\n")
    unit_dir = tmp_path / "systemd"
    unit_dir.mkdir()
    (unit_dir / "controller.service").write_text(
        "[Service]\nExecStart=/opt/controller/old/run.sh\n"
    )
    p = tmp_path / "stack.yaml"
    p.write_text(
        "schema: 1\nservices:\n  controller:\n"
        f"    version: 1.0.0\n    root: {root}\n"
        "    service_file: ./controller.service\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "platformctl.cli.Engine",
        lambda: Engine(unit_files=UnitFileManager(unit_dir)),
    )

    assert main(["diff", "-f", str(p)]) == 0
    output = capsys.readouterr().out
    assert "systemd unit controller.service will be updated" in output
    assert "No managed version" not in output
