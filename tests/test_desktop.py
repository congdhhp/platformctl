from __future__ import annotations

from pathlib import Path

import pytest

from platformctl.cli import main
from platformctl.desktop_files import DesktopFileManager
from platformctl.engine import Engine
from platformctl.errors import ApplyError, DesktopError, ManifestError
from platformctl.manifest import load_manifest
from platformctl.models import ServiceSpec, StackManifest
from platformctl.release import current_version, previous_version


def launcher_text(version_marker: str = "Stable GUI") -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={version_marker}\n"
        "Exec=/opt/Briefcase/current/software/bin/launch.sh\n"
        "Path=/opt/Briefcase/current/software/bin\n"
        "Icon=/opt/Briefcase/current/software/icons/app.svg\n"
        "Terminal=false\n"
        "Categories=Utility;\n"
    )


def make_desktop(tmp_path: Path, *, desired: str = "2.0.0",
                 installed: bool = True) -> tuple[ServiceSpec, DesktopFileManager, Path]:
    root = tmp_path / "Briefcase"
    (root / "1.0.0").mkdir(parents=True)
    (root / "2.0.0").mkdir()
    (root / "current").symlink_to(root / "1.0.0")
    source = tmp_path / "VmlrsBriefcaseSoftware.desktop"
    source.write_text(launcher_text(), encoding="utf-8")
    destination = tmp_path / "applications"
    if installed:
        destination.mkdir()
    spec = ServiceSpec(
        name="briefcase", version=desired, root=root,
        service_name="briefcase.service", type="desktop", desktop_file=source,
    )
    return spec, DesktopFileManager(destination), destination / source.name


class NoSystemd:
    def __getattr__(self, method):
        raise AssertionError(f"desktop must not invoke systemd.{method}")


def test_desktop_manifest_and_backwards_compatible_service(tmp_path: Path):
    (tmp_path / "desktops").mkdir()
    stack = tmp_path / "stack.yaml"
    stack.write_text(
        "schema: 1\nservices:\n"
        "  controller:\n    version: 1.0.0\n"
        "  briefcase:\n    type: desktop\n    version: 2.0.0\n"
        "    desktop_file: ./desktops/VmlrsBriefcaseSoftware.desktop\n"
    )
    specs = load_manifest(stack).services
    assert specs["controller"].type == "service"
    assert specs["controller"].desktop_file is None
    assert specs["controller"].state == "running"
    assert specs["briefcase"].type == "desktop"
    assert specs["briefcase"].desktop_file == (
        tmp_path / "desktops/VmlrsBriefcaseSoftware.desktop"
    )


@pytest.mark.parametrize("bad", [
    "type: desktop\n    state: running",
    "type: desktop\n    autostart: false",
    "type: desktop\n    enabled: true",
    "type: desktop\n    service_name: briefcase.service",
    "type: desktop\n    service_file: ./services/a.service",
    "type: desktop\n    install_path: /opt/vendor/briefcase",
    "type: desktop",
    "type: desktop\n    desktop_file: ''",
    "type: service\n    desktop_file: ./briefcase.desktop",
    "type: hybrid",
])
def test_manifest_rejects_invalid_desktop_configuration(tmp_path: Path, bad: str):
    stack = tmp_path / "stack.yaml"
    stack.write_text(
        "schema: 1\nservices:\n  briefcase:\n"
        "    version: 2.0.0\n    " + bad + "\n"
    )
    with pytest.raises(ManifestError):
        load_manifest(stack)


def test_manifest_rejects_duplicate_launcher_names(tmp_path: Path):
    p = tmp_path / "stack.yaml"
    p.write_text(
        "schema: 1\nservices:\n"
        "  first:\n    type: desktop\n    version: 1.0.0\n"
        "    desktop_file: ./desktop/app.desktop\n"
        "  second:\n    type: desktop\n    version: 2.0.0\n"
        "    desktop_file: ./another/app.desktop\n"
    )
    with pytest.raises(ManifestError, match="same desktop_file name"):
        load_manifest(p)


def test_desktop_file_resolves_from_explicit_project_directory(tmp_path: Path):
    p = tmp_path / "other" / "stack.yaml"
    p.parent.mkdir()
    p.write_text(
        "schema: 1\nservices:\n"
        "  briefcase:\n    type: desktop\n    version: 2.0.0\n"
        "    desktop_file: ./desktops/briefcase.desktop\n"
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    spec = load_manifest(p, project_directory=workspace).services["briefcase"]
    assert spec.desktop_file == workspace / "desktops/briefcase.desktop"


@pytest.mark.parametrize("body, message", [
    ("[Desktop Entry]\nName=Briefcase\nExec=launch\n", "Type=Application"),
    ("[Desktop Entry]\nType=Application\nExec=launch\n", "missing Name"),
    ("[Desktop Entry]\nType=Application\nName=Briefcase\n", "missing Exec"),
    ("[Desktop Entry]\nType=Link\nName=Briefcase\nExec=launch\n", "Type=Application"),
    ("[Other]\nName=Briefcase\nExec=launch\n", "missing [Desktop Entry]"),
    ("[Desktop Entry]\nType=Application\nName=Briefcase\nExec=launch\nHidden=true\n", "Hidden=true"),
])
def test_invalid_desktop_entry_is_rejected_before_changes(
    tmp_path: Path, body: str, message: str
):
    spec, manager, target = make_desktop(tmp_path)
    assert spec.desktop_file is not None
    spec.desktop_file.write_text(body)
    engine = Engine(systemd=NoSystemd(), desktop_files=manager)
    with pytest.raises(DesktopError, match=__import__("re").escape(message)):
        engine.apply_service(spec)
    assert current_version(spec) == "1.0.0"
    assert not target.exists()


def test_desktop_apply_registers_launcher_and_switches_version_without_systemd(
    tmp_path: Path,
):
    spec, manager, target = make_desktop(tmp_path, installed=False)
    engine = Engine(systemd=NoSystemd(), desktop_files=manager)
    manifest = StackManifest(1, tmp_path, {"briefcase": spec})

    engine.validate(manifest)  # doesn't require desired version to be active
    assert engine.apply_service(spec) == ("1.0.0", "2.0.0")
    assert current_version(spec) == "2.0.0"
    assert previous_version(spec) == "1.0.0"
    assert target.read_text() == launcher_text()
    assert target.stat().st_mode & 0o777 == 0o644
    row = engine.status(manifest)[0]
    assert (row.type, row.state, row.enabled) == ("desktop", "registered", None)
    assert engine.apply_service(spec) == ("2.0.0", "2.0.0")
    assert current_version(spec) == "2.0.0"


def test_desktop_rollback_changes_links_only_and_does_not_launch_gui(
    tmp_path: Path,
):
    spec, manager, target = make_desktop(tmp_path)
    engine = Engine(systemd=NoSystemd(), desktop_files=manager)
    engine.apply_service(spec)
    before = target.read_bytes()
    assert engine.rollback(spec) == ("2.0.0", "1.0.0")
    assert current_version(spec) == "1.0.0"
    assert previous_version(spec) == "2.0.0"
    assert target.read_bytes() == before


def test_changed_desktop_file_is_updated_without_version_switch(tmp_path: Path):
    spec, manager, target = make_desktop(tmp_path, desired="1.0.0")
    target.write_text(launcher_text("Old GUI"))
    assert Engine(systemd=NoSystemd(), desktop_files=manager).apply_service(spec) == (
        "1.0.0", "1.0.0"
    )
    assert target.read_text() == launcher_text()
    assert current_version(spec) == "1.0.0"


def test_desktop_apply_failure_restores_launcher_and_both_links(tmp_path: Path):
    spec, manager, target = make_desktop(tmp_path)
    target.write_text(launcher_text("Previous"))
    original = target.read_bytes()

    class FailsAfterWrite(DesktopFileManager):
        def install(self, change):
            super().install(change)
            raise OSError("simulated post-write failure")

    engine = Engine(
        systemd=NoSystemd(),
        desktop_files=FailsAfterWrite(manager.desktop_dir),
    )
    with pytest.raises(ApplyError, match="simulated post-write failure"):
        engine.apply_service(spec)
    assert current_version(spec) == "1.0.0"
    assert not spec.previous_link.exists()
    assert target.read_bytes() == original


def test_reject_symlinked_registered_launcher(tmp_path: Path):
    spec, manager, target = make_desktop(tmp_path)
    original = tmp_path / "victim.desktop"
    original.write_text("do not overwrite")
    target.symlink_to(original)
    with pytest.raises(DesktopError, match="non-regular launcher"):
        Engine(systemd=NoSystemd(), desktop_files=manager).apply_service(spec)
    assert original.read_text() == "do not overwrite"
    assert current_version(spec) == "1.0.0"


def test_status_distinguishes_missing_outdated_and_registered(tmp_path: Path):
    spec, manager, target = make_desktop(tmp_path)
    engine = Engine(systemd=NoSystemd(), desktop_files=manager)
    manifest = StackManifest(1, tmp_path, {"briefcase": spec})
    assert engine.status(manifest)[0].state == "missing"
    target.write_text(launcher_text("Old"))
    assert engine.status(manifest)[0].state == "outdated"
    target.write_text(launcher_text())
    assert engine.status(manifest)[0].state == "registered"


def test_cli_desktop_diff_apply_and_status(tmp_path: Path, monkeypatch, capsys):
    spec, manager, target = make_desktop(tmp_path)
    stack = tmp_path / "stack.yaml"
    stack.write_text(
        "schema: 1\nservices:\n"
        "  briefcase:\n    type: desktop\n    version: 2.0.0\n"
        f"    root: {spec.root}\n"
        "    desktop_file: ./VmlrsBriefcaseSoftware.desktop\n"
    )
    monkeypatch.setattr(
        "platformctl.cli.Engine",
        lambda: Engine(systemd=NoSystemd(), desktop_files=manager),
    )
    assert main(["diff", "-f", str(stack)]) == 0
    assert "desktop launcher" in capsys.readouterr().out
    assert main(["apply", "-f", str(stack)]) == 0
    assert "desktop launcher updated" in capsys.readouterr().out
    assert target.read_text() == launcher_text()
    assert main(["status", "-f", str(stack)]) == 0
    output = capsys.readouterr().out
    assert "desktop" in output and "registered" in output
    assert main(["rollback", "briefcase", "-f", str(stack)]) == 0
    assert "2.0.0 -> 1.0.0" in capsys.readouterr().out
