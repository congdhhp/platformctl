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
    assert "No managed version changes" in output
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
    (root / "releases" / "old").mkdir(parents=True)
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
