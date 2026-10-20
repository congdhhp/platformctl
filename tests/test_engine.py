from pathlib import Path

import pytest

from platformctl.errors import ReleaseError
from platformctl.models import StackManifest

from platformctl.engine import Engine
from platformctl.models import ServiceSpec
from platformctl.release import current_version
from platformctl.unit_files import UnitFileManager


class FakeSystemd:
    def __init__(self):
        self.active = set()
        self.enabled = set()
        self.calls = []

    def daemon_reload(self):
        self.calls.append(("daemon-reload",))

    def is_active(self, unit):
        return unit in self.active

    def is_enabled(self, unit):
        return unit in self.enabled

    def start(self, unit):
        self.calls.append(("start", unit))
        self.active.add(unit)

    def stop(self, unit):
        self.calls.append(("stop", unit))
        self.active.discard(unit)

    def restart(self, unit):
        self.calls.append(("restart", unit))
        self.active.add(unit)

    def enable(self, unit):
        self.calls.append(("enable", unit))
        self.enabled.add(unit)

    def disable(self, unit):
        self.calls.append(("disable", unit))
        self.enabled.discard(unit)


def test_apply_switches_release_and_starts_service(tmp_path: Path):
    root = tmp_path / "softA"
    (root / "1.0.0").mkdir(parents=True)
    (root / "1.1.0").mkdir(parents=True)
    (root / "current").symlink_to(root / "1.0.0")
    spec = ServiceSpec(name="softA", version="1.1.0", service_name="softA.service", root=root)

    systemd = FakeSystemd()
    systemd.active.add("softA.service")
    engine = Engine(systemd=systemd)

    old, new = engine.apply_service(spec)
    assert (old, new) == ("1.0.0", "1.1.0")
    assert current_version(spec) == "1.1.0"
    assert ("stop", "softA.service") in systemd.calls
    assert ("start", "softA.service") in systemd.calls

def test_external_apply_reconciles_systemd_only(tmp_path: Path):
    install = tmp_path / "vendor" / "softA"
    install.mkdir(parents=True)
    spec = ServiceSpec(
        name="softA", version="1.2.3", service_name="softA.service",
        root=None, install_path=install
    )
    systemd = FakeSystemd()
    systemd.active.add(spec.service_name)
    engine = Engine(systemd=systemd)

    assert engine.apply_service(spec) == (None, "1.2.3")
    assert ("enable", spec.service_name) in systemd.calls
    assert all(call[0] not in {"stop", "start", "restart"} for call in systemd.calls)
    assert list(install.iterdir()) == []  # Never create or switch symlinks.

    statuses = engine.status(
        StackManifest(schema=1, platform_root=tmp_path, services={spec.name: spec})
    )
    assert statuses[0].current is None
    assert statuses[0].mode == "external"


def test_external_apply_stops_service(tmp_path: Path):
    install = tmp_path / "legacy"
    install.mkdir()
    spec = ServiceSpec(
        name="legacy", version="1.2.3", service_name="legacy.service",
        root=None, install_path=install, state="stopped", autostart=False,
    )
    systemd = FakeSystemd()
    systemd.active.add(spec.service_name)
    engine = Engine(systemd=systemd)
    engine.apply_service(spec)

    assert ("stop", spec.service_name) in systemd.calls
    assert ("disable", spec.service_name) in systemd.calls
    assert not systemd.is_active(spec.service_name)


def test_external_validate_requires_existing_directory(tmp_path: Path):
    spec = ServiceSpec(
        name="legacy", version="1.0.0", service_name="legacy.service",
        root=None, install_path=tmp_path / "missing",
    )
    manifest = StackManifest(
        schema=1, platform_root=tmp_path, services={spec.name: spec}
    )
    with pytest.raises(ReleaseError, match="installation directory not found"):
        Engine(systemd=FakeSystemd()).validate(manifest)


def test_external_rollback_rejected_before_stopping_service(tmp_path: Path):
    install = tmp_path / "legacy"
    install.mkdir()
    spec = ServiceSpec(
        name="legacy", version="1.2.3", service_name="legacy.service",
        root=None, install_path=install,
    )
    systemd = FakeSystemd()
    systemd.active.add(spec.service_name)
    with pytest.raises(ReleaseError, match="rollback is unavailable"):
        Engine(systemd=systemd).rollback(spec)
    assert systemd.calls == []


def test_invalid_symlink_is_rejected_before_stopping_unit(tmp_path: Path):
    root = tmp_path / "softA"
    (root / "1.1.0").mkdir(parents=True)
    outside = tmp_path / "outside" / "1.0.0"
    outside.mkdir(parents=True)
    (root / "current").symlink_to(outside)
    spec = ServiceSpec(name="softA", version="1.1.0", service_name="softA.service", root=root)
    systemd = FakeSystemd()
    systemd.active.add(spec.service_name)
    engine = Engine(systemd=systemd)

    with pytest.raises(ReleaseError, match="invalid current symlink"):
        engine.apply_service(spec)
    assert systemd.is_active(spec.service_name)
    assert not systemd.calls


def test_validate_accepts_flat_directory(tmp_path: Path):
    root = tmp_path / "softA"
    (root / "2.5.5-build184").mkdir(parents=True)
    spec = ServiceSpec(
        name="softA", version="2.5.5-build184",
        service_name="softA.service", root=root,
    )
    Engine(systemd=FakeSystemd()).validate(
        StackManifest(schema=1, platform_root=tmp_path, services={"softA": spec})
    )


def _unit_fixture(tmp_path: Path, *, installed: bool = True):
    root = tmp_path / "softA"
    (root / "1.0.0").mkdir(parents=True)
    (root / "1.1.0").mkdir()
    (root / "current").symlink_to(root / "1.0.0")
    source = tmp_path / "override.service"
    source.write_text(
        "[Unit]\nDescription=New\n[Service]\nExecStart=/opt/softA/current/run.sh\n",
        encoding="utf-8",
    )
    units = tmp_path / "systemd"
    units.mkdir()
    target = units / "softA.service"
    if installed:
        target.write_text(
            "[Unit]\nDescription=Original\n[Service]\nExecStart=/opt/softA/current/run.sh\n",
            encoding="utf-8",
        )
    spec = ServiceSpec(
        name="softA", version="1.0.0",
        service_name="softA.service", root=root, service_file=source,
    )
    return spec, source, target, UnitFileManager(units)


def test_service_file_only_change_restarts_running_unit(tmp_path: Path):
    spec, _, target, manager = _unit_fixture(tmp_path)
    systemd = FakeSystemd()
    systemd.active.add(spec.service_name)
    engine = Engine(systemd=systemd, unit_files=manager)
    old, new = engine.apply_service(spec)

    assert (old, new) == ("1.0.0", "1.0.0")
    assert "Description=New" in target.read_text()
    assert ("daemon-reload",) in systemd.calls
    assert ("stop", spec.service_name) in systemd.calls
    assert ("start", spec.service_name) in systemd.calls
    assert current_version(spec) == "1.0.0"


def test_service_file_identical_does_not_restart(tmp_path: Path):
    spec, source, target, manager = _unit_fixture(tmp_path)
    target.write_bytes(source.read_bytes())
    systemd = FakeSystemd()
    systemd.active.add(spec.service_name)
    engine = Engine(systemd=systemd, unit_files=manager)
    engine.apply_service(spec)

    assert ("daemon-reload",) not in systemd.calls
    assert ("stop", spec.service_name) not in systemd.calls
    assert ("start", spec.service_name) not in systemd.calls


def test_service_file_missing_target_is_installed(tmp_path: Path):
    spec, _, target, manager = _unit_fixture(tmp_path, installed=False)
    systemd = FakeSystemd()
    Engine(systemd=systemd, unit_files=manager).apply_service(spec)

    assert "Description=New" in target.read_text()
    assert systemd.is_active(spec.service_name)
    assert ("daemon-reload",) in systemd.calls


def test_service_file_updates_without_starting_stopped_service(tmp_path: Path):
    spec, _, target, manager = _unit_fixture(tmp_path)
    spec = ServiceSpec(
        name=spec.name, version=spec.version, service_name=spec.service_name,
        root=spec.root, service_file=spec.service_file,
        state="stopped", autostart=False,
    )
    systemd = FakeSystemd()
    Engine(systemd=systemd, unit_files=manager).apply_service(spec)

    assert "Description=New" in target.read_text()
    assert ("start", spec.service_name) not in systemd.calls
    assert ("daemon-reload",) in systemd.calls


class FailOnceStartSystemd(FakeSystemd):
    def __init__(self):
        super().__init__()
        self.fail_next_start = True

    def start(self, unit):
        if self.fail_next_start:
            self.fail_next_start = False
            self.calls.append(("start-failed", unit))
            raise RuntimeError("simulated start failure")
        return super().start(unit)


def test_failed_restart_restores_unit_and_both_links(tmp_path: Path):
    from platformctl.errors import ApplyError

    spec, _, target, manager = _unit_fixture(tmp_path)
    spec = ServiceSpec(
        name=spec.name, version="1.1.0",
        service_name=spec.service_name, root=spec.root,
        service_file=spec.service_file,
    )
    old_content = target.read_bytes()
    systemd = FailOnceStartSystemd()
    systemd.active.add(spec.service_name)
    systemd.enabled.add(spec.service_name)
    engine = Engine(systemd=systemd, unit_files=manager)

    with pytest.raises(ApplyError, match="simulated start failure"):
        engine.apply_service(spec)

    assert target.read_bytes() == old_content
    assert current_version(spec) == "1.0.0"
    assert not spec.previous_link.exists()
    assert systemd.is_active(spec.service_name)
    assert systemd.calls.count(("daemon-reload",)) == 2


def test_external_service_file_can_be_overridden(tmp_path: Path):
    spec, _, target, manager = _unit_fixture(tmp_path)
    external = tmp_path / "vendor"
    external.mkdir()
    spec = ServiceSpec(
        name="softA", version="9.0", service_name=spec.service_name,
        root=None, install_path=external, service_file=spec.service_file,
    )
    systemd = FakeSystemd()
    Engine(systemd=systemd, unit_files=manager).apply_service(spec)
    assert "Description=New" in target.read_text()
    assert systemd.is_active(spec.service_name)
    assert list(external.iterdir()) == []


def test_invalid_unit_file_does_not_stop_running_service(tmp_path: Path):
    spec, source, target, manager = _unit_fixture(tmp_path)
    source.write_text("[Unit]\nDescription=missing service section\n")
    systemd = FakeSystemd()
    systemd.active.add(spec.service_name)
    old = target.read_bytes()
    from platformctl.errors import SystemdError
    with pytest.raises(SystemdError, match="missing \\[Service\\]"):
        Engine(systemd=systemd, unit_files=manager).apply_service(spec)
    assert target.read_bytes() == old
    assert systemd.calls == []


def test_refuse_to_overwrite_symlinked_installed_unit(tmp_path: Path):
    spec, _, target, manager = _unit_fixture(tmp_path, installed=False)
    actual = tmp_path / "vendor.service"
    actual.write_text("[Service]\nExecStart=/opt/vendor/run.sh\n")
    target.symlink_to(actual)
    systemd = FakeSystemd()
    systemd.active.add(spec.service_name)
    from platformctl.errors import SystemdError
    with pytest.raises(SystemdError, match="non-regular unit"):
        Engine(systemd=systemd, unit_files=manager).apply_service(spec)
    assert actual.read_text().startswith("[Service]")
    assert systemd.calls == []


def test_no_service_file_keeps_installed_unit_untouched(tmp_path: Path):
    spec, _, target, manager = _unit_fixture(tmp_path)
    bare = ServiceSpec(
        name=spec.name, version=spec.version,
        service_name=spec.service_name, root=spec.root,
    )
    original = target.read_bytes()
    systemd = FakeSystemd()
    systemd.active.add(bare.service_name)
    Engine(systemd=systemd, unit_files=manager).apply_service(bare)
    assert target.read_bytes() == original
    assert ("daemon-reload",) not in systemd.calls
