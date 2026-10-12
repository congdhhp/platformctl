from pathlib import Path

from platformctl.models import ServiceSpec
from platformctl.release import activate, current_version, rollback_link


def spec(tmp_path: Path) -> ServiceSpec:
    root = tmp_path / "softA"
    (root / "releases" / "1.0.0").mkdir(parents=True)
    (root / "releases" / "1.1.0").mkdir(parents=True)
    return ServiceSpec(name="softA", version="1.0.0", unit="softA.service", root=root)


def test_activate_and_rollback(tmp_path: Path):
    s = spec(tmp_path)
    activate(s, "1.0.0")
    assert current_version(s) == "1.0.0"

    activate(s, "1.1.0")
    assert current_version(s) == "1.1.0"

    rollback_link(s)
    assert current_version(s) == "1.0.0"
