from pathlib import Path

import pytest

from platformctl.errors import ReleaseError
from platformctl.models import StackManifest

from platformctl.engine import Engine
from platformctl.models import ServiceSpec
from platformctl.release import current_version


class FakeSystemd:
    def __init__(self):
        self.active = set()
        self.enabled = set()
        self.calls = []

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
    spec = ServiceSpec(name="softA", version="1.1.0", unit="softA.service", root=root)

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
        name="softA", version="1.2.3", unit="softA.service",
        root=None, install_path=install
    )
    systemd = FakeSystemd()
    systemd.active.add(spec.unit)
    engine = Engine(systemd=systemd)

    assert engine.apply_service(spec) == (None, "1.2.3")
    assert ("enable", spec.unit) in systemd.calls
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
        name="legacy", version="1.2.3", unit="legacy.service",
        root=None, install_path=install, state="stopped", autostart=False,
    )
    systemd = FakeSystemd()
    systemd.active.add(spec.unit)
    engine = Engine(systemd=systemd)
    engine.apply_service(spec)

    assert ("stop", spec.unit) in systemd.calls
    assert ("disable", spec.unit) in systemd.calls
    assert not systemd.is_active(spec.unit)


def test_external_validate_requires_existing_directory(tmp_path: Path):
    spec = ServiceSpec(
        name="legacy", version="1.0.0", unit="legacy.service",
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
        name="legacy", version="1.2.3", unit="legacy.service",
        root=None, install_path=install,
    )
    systemd = FakeSystemd()
    systemd.active.add(spec.unit)
    with pytest.raises(ReleaseError, match="rollback is unavailable"):
        Engine(systemd=systemd).rollback(spec)
    assert systemd.calls == []


def test_invalid_symlink_is_rejected_before_stopping_unit(tmp_path: Path):
    root = tmp_path / "softA"
    (root / "1.1.0").mkdir(parents=True)
    outside = tmp_path / "outside" / "1.0.0"
    outside.mkdir(parents=True)
    (root / "current").symlink_to(outside)
    spec = ServiceSpec(name="softA", version="1.1.0", unit="softA.service", root=root)
    systemd = FakeSystemd()
    systemd.active.add(spec.unit)
    engine = Engine(systemd=systemd)

    with pytest.raises(ReleaseError, match="invalid current symlink"):
        engine.apply_service(spec)
    assert systemd.is_active(spec.unit)
    assert not systemd.calls


def test_validate_accepts_flat_directory(tmp_path: Path):
    root = tmp_path / "softA"
    (root / "2.5.5-build184").mkdir(parents=True)
    spec = ServiceSpec(
        name="softA", version="2.5.5-build184",
        unit="softA.service", root=root,
    )
    Engine(systemd=FakeSystemd()).validate(
        StackManifest(schema=1, platform_root=tmp_path, services={"softA": spec})
    )
