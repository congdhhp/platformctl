from __future__ import annotations

from pathlib import Path

import pytest

from platformctl.artifacts import artifact_for
from platformctl.cli import main
from platformctl.errors import ManifestError
from platformctl.manifest import load_manifest


def make_stack(folder: Path, *, global_source: str | None = None,
               source: str | None = "controller/",
               service_file: str | None = None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    manifest = folder / "stack.yaml"
    lines = ["schema: 1", "platform:", "  root: /opt/platform"]
    if global_source is not None:
        lines.append(f"  source: '{global_source}'")
    lines += [
        "services:", "  controller:", "    version: 2.5.5-build184",
    ]
    if source is not None:
        lines.append(f"    source: '{source}'")
    if service_file is not None:
        lines.append(f"    service_file: '{service_file}'")
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return manifest


def test_global_nexus_directory_convention(tmp_path: Path):
    path = make_stack(
        tmp_path, global_source="http://nexus.lan:8081/repository/native-releases/",
        source="controller/",
    )
    stack = load_manifest(path)
    assert stack.global_source == "http://nexus.lan:8081/repository/native-releases/"
    assert stack.services["controller"].source == stack.global_source + "controller/"
    assert artifact_for(stack.services["controller"]).location == (
        "http://nexus.lan:8081/repository/native-releases/controller/"
        "2.5.5-build184/controller-2.5.5-build184.deb"
    )


def test_relative_direct_deb_under_global_source(tmp_path: Path):
    path = make_stack(
        tmp_path, global_source="https://repo.example/releases/",
        source="controller/2.5.5-build184/controller.deb",
    )
    spec = load_manifest(path).services["controller"]
    assert artifact_for(spec).location == (
        "https://repo.example/releases/controller/2.5.5-build184/controller.deb"
    )


@pytest.mark.parametrize("source", ["controller/", "./controller/"])
def test_dot_slash_does_not_override_global_source(tmp_path: Path, source: str):
    stack = load_manifest(
        make_stack(tmp_path, global_source="https://repo.example/releases/", source=source)
    )
    assert stack.services["controller"].source == "https://repo.example/releases/controller/"


def test_global_absolute_local_directory(tmp_path: Path):
    store = tmp_path / "store"
    stack = load_manifest(make_stack(tmp_path / "project", global_source=str(store) + "/"))
    assert stack.global_source == str(store.resolve()) + "/"
    assert artifact_for(stack.services["controller"]).location == str(
        store / "controller" / "2.5.5-build184" / "controller-2.5.5-build184.deb"
    )


def test_global_relative_local_directory_uses_project_dir_not_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    elsewhere = tmp_path / "cwd"
    elsewhere.mkdir()
    project = tmp_path / "deployment"
    manifest = make_stack(project, global_source="./native-releases/")
    monkeypatch.chdir(elsewhere)
    stack = load_manifest(manifest)
    assert stack.project_directory == project
    assert stack.global_source == str(project / "native-releases") + "/"
    assert artifact_for(stack.services["controller"]).location == str(
        project / "native-releases" / "controller" / "2.5.5-build184"
        / "controller-2.5.5-build184.deb"
    )


def test_without_global_relative_service_source_uses_manifest_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    elsewhere = tmp_path / "cwd"
    elsewhere.mkdir()
    project = tmp_path / "deployment"
    manifest = make_stack(project, source="./packages/controller/")
    monkeypatch.chdir(elsewhere)
    spec = load_manifest(manifest).services["controller"]
    assert spec.source == str(project / "packages" / "controller") + "/"


def test_without_global_relative_direct_file_backward_compatible(tmp_path: Path):
    project = tmp_path / "deployment"
    manifest = make_stack(project, source="./packages/controller.deb")
    spec = load_manifest(manifest).services["controller"]
    assert artifact_for(spec).location == str(project / "packages" / "controller.deb")


def test_explicit_project_directory_overrides_both_local_sources_and_service_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    project = tmp_path / "deployment"
    workspace = tmp_path / "jenkins"
    workspace.mkdir()
    manifest = make_stack(
        project, global_source="./native-releases/",
        service_file="./services/controller.service",
    )
    another = tmp_path / "other"
    another.mkdir()
    monkeypatch.chdir(another)
    stack = load_manifest(manifest, project_directory=workspace)
    assert stack.project_directory == workspace
    assert stack.global_source == str(workspace / "native-releases") + "/"
    assert stack.services["controller"].service_file == (
        workspace / "services" / "controller.service"
    )
    assert stack.services["controller"].source == (
        str(workspace / "native-releases" / "controller") + "/"
    )


def test_relative_project_directory_option_is_resolved_from_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    project = tmp_path / "deployment"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manifest = make_stack(project, source="./packages/controller/")
    monkeypatch.chdir(tmp_path)
    stack = load_manifest(manifest, project_directory="workspace")
    assert stack.project_directory == workspace
    assert stack.services["controller"].source == str(workspace / "packages" / "controller") + "/"


def test_absolute_remote_and_local_overrides_ignore_global(tmp_path: Path):
    global_source = "https://repo.example/releases/"
    remote = load_manifest(
        make_stack(tmp_path / "remote", global_source=global_source,
                   source="https://override.example/controller.deb")
    )
    assert artifact_for(remote.services["controller"]).location == (
        "https://override.example/controller.deb"
    )
    local = load_manifest(
        make_stack(tmp_path / "local", global_source=global_source,
                   source="/mnt/override/controller.deb")
    )
    assert artifact_for(local.services["controller"]).location == (
        "/mnt/override/controller.deb"
    )


def test_missing_service_source_keeps_preinstalled_mode(tmp_path: Path):
    stack = load_manifest(
        make_stack(tmp_path, global_source="https://repo.example/releases/", source=None)
    )
    assert stack.services["controller"].source is None


@pytest.mark.parametrize("bad_global", [
    "https://nexus.example/controller.deb",
    "/mnt/packages/controller.deb",
    "ftp://repo.example/releases/",
    "https://user:password@repo.example/releases/",
    "https://repo.example/releases/?token=abc",
    "relative-without-trailing-slash",
    "",
])
def test_reject_invalid_global_sources(tmp_path: Path, bad_global: str):
    path = make_stack(tmp_path, global_source=bad_global)
    with pytest.raises(ManifestError):
        load_manifest(path)


@pytest.mark.parametrize("bad_relative", [
    "../other/controller/", "./../other/controller/",
    "controller/../../private.deb", "ftp://example.com/controller.deb",
])
def test_reject_relative_escape_or_unsupported_scheme_with_global(
    tmp_path: Path, bad_relative: str
):
    path = make_stack(
        tmp_path, global_source="https://repo.example/releases/",
        source=bad_relative,
    )
    with pytest.raises(ManifestError):
        load_manifest(path)


def test_existing_no_global_parent_path_behavior_preserved(tmp_path: Path):
    project = tmp_path / "deployment"
    path = make_stack(project, source="../shared/controller/")
    spec = load_manifest(path).services["controller"]
    assert spec.source == str(tmp_path / "shared" / "controller") + "/"


def test_invalid_project_directory(tmp_path: Path):
    path = make_stack(tmp_path / "project")
    with pytest.raises(ManifestError, match="project directory"):
        load_manifest(path, project_directory=tmp_path / "not-a-directory")


def test_cli_show_sources_does_not_require_files_or_network(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    path = make_stack(
        tmp_path, global_source="https://repo.example/releases/",
        source="controller/1.0/controller.deb",
    )
    assert main(["validate", "-f", str(path), "--show-sources"]) == 0
    output = capsys.readouterr().out
    assert "Project directory:" in output
    assert "Global source: https://repo.example/releases/" in output
    assert "https://repo.example/releases/controller/1.0/controller.deb" in output
    assert output.strip().endswith("OK")


def test_cli_project_directory_override_and_preinstalled_display(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    project = tmp_path / "project"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manifest = make_stack(project, source="./packages/controller.deb")
    assert main([
        "validate", "-f", str(manifest), "--project-directory", str(workspace),
        "--show-sources",
    ]) == 0
    assert str(workspace / "packages" / "controller.deb") in capsys.readouterr().out

    no_source = make_stack(project, source=None)
    assert main(["validate", "-f", str(no_source), "--show-sources"]) == 0
    assert "(pre-installed; no source)" in capsys.readouterr().out


def test_cli_project_directory_accepted_for_other_commands(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    manifest = make_stack(tmp_path / "project", source=None)
    root = tmp_path / "workspace"
    root.mkdir()
    assert main([
        "check", "-f", str(manifest), "--project-directory", str(root),
    ]) == 2
    assert "MISSING" in capsys.readouterr().out


def test_cli_project_directory_can_precede_subcommand(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    manifest = make_stack(tmp_path / "project", source="./pkg/controller.deb")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    assert main([
        "--project-directory", str(workspace),
        "validate", "-f", str(manifest), "--show-sources",
    ]) == 0
    assert str(workspace / "pkg" / "controller.deb") in capsys.readouterr().out


def test_local_global_source_rejects_symlink_escaping_base(tmp_path: Path):
    store = tmp_path / "store"
    store.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (store / "controller").symlink_to(outside, target_is_directory=True)
    manifest = make_stack(tmp_path / "project", global_source=str(store) + "/")
    with pytest.raises(ManifestError, match="outside platform.source"):
        load_manifest(manifest)
