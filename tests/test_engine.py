from pathlib import Path

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
    (root / "releases" / "1.0.0").mkdir(parents=True)
    (root / "releases" / "1.1.0").mkdir(parents=True)
    (root / "current").symlink_to(root / "releases" / "1.0.0")
    spec = ServiceSpec(name="softA", version="1.1.0", unit="softA.service", root=root)

    systemd = FakeSystemd()
    systemd.active.add("softA.service")
    engine = Engine(systemd=systemd)

    old, new = engine.apply_service(spec)
    assert (old, new) == ("1.0.0", "1.1.0")
    assert current_version(spec) == "1.1.0"
    assert ("stop", "softA.service") in systemd.calls
    assert ("start", "softA.service") in systemd.calls
