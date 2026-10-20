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

def test_external_has_no_managed_release(tmp_path: Path):
    import pytest
    from platformctl.errors import ReleaseError
    from platformctl.release import ensure_release_exists, previous_version

    path = tmp_path / "vendor"
    path.mkdir()
    s = ServiceSpec(
        name="vendor", version="1.0.0", unit="vendor.service",
        root=None, install_path=path,
    )
    assert current_version(s) is None
    assert previous_version(s) is None
    with pytest.raises(ReleaseError, match="no managed releases"):
        ensure_release_exists(s)
    with pytest.raises(ReleaseError, match="rollback is unavailable"):
        rollback_link(s)
