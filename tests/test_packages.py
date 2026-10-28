from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from platformctl.artifacts import resolve_source
from platformctl.errors import ManifestError, PackageError
from platformctl.manifest import load_manifest
from platformctl.models import ServiceSpec
from platformctl.packages import PackageManager, expected_package_name


def _spec(tmp_path: Path, *, source: str | None = None) -> ServiceSpec:
    return ServiceSpec(
        name="controller", version="2.5.5-build184",
        service_name="controller.service",
        root=tmp_path / "ControllerSoftware", source=source,
    )


def _deb(
    tmp_path: Path, *, name: str = "controller-release-2.5.5-build184",
    version: str = "2.5.5-build184", postinst: bool = False,
    outside: bool = False, escape_link: bool = False,
) -> Path:
    if shutil.which("dpkg-deb") is None:
        pytest.skip("Debian package tooling not installed")
    data = tmp_path / "deb-data"
    control = data / "DEBIAN"
    control.mkdir(parents=True)
    (control / "control").write_text(
        f"Package: {name}\n"
        f"Version: {version}\n"
        "Architecture: all\nMaintainer: Test <test@example.com>\n"
        "Description: test versioned application\n",
        encoding="utf-8",
    )
    if postinst:
        script = control / "postinst"
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(0o755)
    root = data / str(tmp_path / "ControllerSoftware").lstrip("/") / version
    root.mkdir(parents=True)
    (root / "run.sh").write_text("#!/bin/sh\nexit 0\n")
    if outside:
        shared = data / "etc" / "systemd" / "system"
        shared.mkdir(parents=True)
        (shared / "controller.service").write_text("[Service]\nExecStart=/bin/true\n")
    if escape_link:
        (root / "unsafe").symlink_to("/etc/passwd")
    target = tmp_path / "controller.deb"
    result = subprocess.run(
        ["dpkg-deb", "--build", str(data), str(target)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    return target


def test_direct_and_directory_sources():
    version = "2.5.5-build184"
    assert resolve_source(
        "controller", version,
        "http://nexus.lan:8081/repository/native-releases/controller/",
    ).location.endswith(
        "/controller/2.5.5-build184/controller-2.5.5-build184.deb"
    )
    assert resolve_source(
        "controller", version, "https://repo.lan/controller/explicit.deb"
    ).location == "https://repo.lan/controller/explicit.deb"
    assert resolve_source(
        "controller", version, "/mnt/packages/controller/"
    ).location == (
        "/mnt/packages/controller/2.5.5-build184/controller-2.5.5-build184.deb"
    )
    assert resolve_source(
        "controller", version, "/mnt/packages/controller.deb"
    ).location == "/mnt/packages/controller.deb"


@pytest.mark.parametrize("source", [
    "ftp://server/controller/",
    "http://nexus.lan/repository/controller",
    "https://user:secret@server/repo/",
    "http://server/repo/?auth=123",
    "local-dir-no-slash",
])
def test_source_rejects_ambiguous_or_unsupported_inputs(source: str):
    with pytest.raises(ManifestError):
        resolve_source("controller", "2.5.5-build184", source)


def test_manifest_normalizes_relative_local_source(tmp_path: Path):
    p = tmp_path / "stack.yaml"
    p.write_text(
        "schema: 1\nservices:\n"
        "  controller:\n    version: 2.5.5-build184\n"
        "    source: ./packages/controller/\n",
        encoding="utf-8",
    )
    spec = load_manifest(p).services["controller"]
    assert spec.source == str((tmp_path / "packages/controller").resolve()) + "/"


def test_manifest_allows_missing_release_but_not_source_in_external_mode(tmp_path: Path):
    p = tmp_path / "stack.yaml"
    p.write_text(
        "schema: 1\nservices:\n  controller:\n"
        "    version: 2.5.5-build184\n"
        "    source: https://nexus.lan/controller/\n",
        encoding="utf-8",
    )
    assert load_manifest(p).services["controller"].source.endswith("/controller/")
    p.write_text(
        "schema: 1\nservices:\n  controller:\n"
        "    version: 2.5.5-build184\n"
        "    install_path: /opt/vendor/controller\n"
        "    source: https://nexus.lan/controller/\n",
        encoding="utf-8",
    )
    with pytest.raises(ManifestError, match="unsupported with install_path"):
        load_manifest(p)


def test_deb_preflight_accepts_valid_coinstallable_release(tmp_path: Path):
    spec = _spec(tmp_path)
    deb = _deb(tmp_path)
    with __import__("tempfile").TemporaryDirectory() as scratch:
        PackageManager()._inspect(spec, deb, Path(scratch))


@pytest.mark.parametrize("bad", [
    {"name": "controller"},
    {"version": "2.5.4-build178"},
    {"postinst": True},
    {"outside": True},
    {"escape_link": True},
])
def test_deb_preflight_rejects_unsafe_payload(tmp_path: Path, bad):
    spec = _spec(tmp_path)
    deb = _deb(tmp_path, **bad)
    with __import__("tempfile").TemporaryDirectory() as scratch:
        with pytest.raises(PackageError):
            PackageManager()._inspect(spec, deb, Path(scratch))


def test_install_does_not_activate_or_modify_service(tmp_path: Path, monkeypatch):
    source = _deb(tmp_path)
    spec = _spec(tmp_path, source=str(source))
    root = spec.version_root
    old = root / "2.5.4-build178"
    old.mkdir(parents=True)
    (root / "current").symlink_to(old)
    package = expected_package_name(spec)

    manager = PackageManager()
    installed = False
    original_run = manager._run
    calls = []

    def fake_run(*args, check=True):
        nonlocal installed
        if args[0] == "dpkg-query":
            status = "install ok installed" if installed else "not-installed"
            return subprocess.CompletedProcess(
                args, 0 if installed else 1,
                f"{status}\t{spec.version}" if installed else "",
                "",
            )
        if args[:2] == ("dpkg", "-i"):
            calls.append(args)
            (root / spec.version).mkdir()
            installed = True
            return subprocess.CompletedProcess(args, 0, "", "")
        return original_run(*args, check=check)

    monkeypatch.setattr(manager, "_run", fake_run)
    assert manager.install(spec).startswith("installed ")
    assert (root / "current").resolve() == old
    assert not (root / "previous").exists()
    assert len(calls) == 1
    assert manager.check(spec)[0] is True
    assert manager.install(spec) == "already installed"
    assert len(calls) == 1


def test_check_is_read_only_and_reports_missing_or_untracked(tmp_path: Path, monkeypatch):
    manager = PackageManager()
    spec = _spec(tmp_path, source="/mnt/packages/controller/")
    ok, detail = manager.check(spec)
    assert not ok and "not deployed" in detail

    (spec.version_root / spec.version).mkdir(parents=True)
    monkeypatch.setattr(manager, "installed_package_version", lambda _: None)
    ok, detail = manager.check(spec)
    assert not ok and "dpkg package" in detail

    legacy = _spec(tmp_path)
    ok, detail = manager.check(legacy)
    assert ok and "release directory" in detail


def test_untracked_directory_is_not_overwritten(tmp_path: Path, monkeypatch):
    spec = _spec(tmp_path, source="/mnt/packages/controller/")
    (spec.version_root / spec.version).mkdir(parents=True)
    manager = PackageManager()
    monkeypatch.setattr(manager, "installed_package_version", lambda _: None)
    with pytest.raises(PackageError, match="refusing to overwrite"):
        manager.install(spec)


def test_dpkg_name_convention(tmp_path: Path):
    assert expected_package_name(_spec(tmp_path)) == "controller-release-2.5.5-build184"


def test_http_fetch_streams_to_local_file(tmp_path: Path, monkeypatch):
    import io
    from platformctl import packages

    requested = []

    def fake_urlopen(request, timeout):
        requested.append((request.full_url, timeout))
        return io.BytesIO(b"debian-package-bytes")

    monkeypatch.setattr(packages.urllib.request, "urlopen", fake_urlopen)
    output = tmp_path / "download.deb"
    PackageManager._download("https://repo.lan/controller.deb", output)

    assert output.read_bytes() == b"debian-package-bytes"
    assert requested == [("https://repo.lan/controller.deb", 60)]


def test_apply_rejects_unregistered_package_before_touching_systemd(tmp_path: Path):
    from platformctl.engine import Engine
    from platformctl.errors import ReleaseError

    spec = _spec(tmp_path, source="/mnt/packages/controller/")
    (spec.version_root / spec.version).mkdir(parents=True)

    class SystemdMustNotBeCalled:
        def is_active(self, unit):
            raise AssertionError("systemd must not be touched")
        def is_enabled(self, unit):
            raise AssertionError("systemd must not be touched")

    class UnreadyPackages(PackageManager):
        def check(self, spec):
            return False, "dpkg package not installed"

    engine = Engine(systemd=SystemdMustNotBeCalled(), packages=UnreadyPackages())
    with pytest.raises(ReleaseError, match="not ready for activation"):
        engine.apply_service(spec)
    assert not spec.current_link.exists()
