from pathlib import Path

import pytest

from platformctl.models import ServiceSpec

from platformctl.errors import ReleaseError
from platformctl.release import (
    activate,
    current_version,
    ensure_release_exists,
    list_deployed_releases,
    previous_version,
    release_path,
    rollback_link,
    validate_managed_links,
)


def spec(tmp_path: Path) -> ServiceSpec:
    root = tmp_path / "softA"
    (root / "1.0.0").mkdir(parents=True)
    (root / "1.1.0").mkdir(parents=True)
    return ServiceSpec(name="softA", version="1.0.0", service_name="softA.service", root=root)


def test_activate_and_rollback(tmp_path: Path):
    s = spec(tmp_path)
    assert release_path(s) == s.root / "1.0.0"
    activate(s, "1.0.0")
    assert current_version(s) == "1.0.0"
    assert s.current_link.resolve() == s.root / "1.0.0"

    activate(s, "1.1.0")
    assert current_version(s) == "1.1.0"
    assert previous_version(s) == "1.0.0"
    assert s.previous_link.resolve() == s.root / "1.0.0"

    rollback_link(s)
    assert current_version(s) == "1.0.0"

def test_external_has_no_managed_release(tmp_path: Path):
    path = tmp_path / "vendor"
    path.mkdir()
    s = ServiceSpec(
        name="vendor", version="1.0.0", service_name="vendor.service",
        root=None, install_path=path,
    )
    assert current_version(s) is None
    assert previous_version(s) is None
    with pytest.raises(ReleaseError, match="no managed releases"):
        ensure_release_exists(s)
    with pytest.raises(ReleaseError, match="rollback is unavailable"):
        rollback_link(s)


def test_flat_list_ignores_symlinks_and_non_version_paths(tmp_path: Path):
    s = spec(tmp_path)
    (s.root / ".staging").mkdir()
    (s.root / "notes.txt").write_text("not a version")
    activate(s, "1.0.0")
    activate(s, "1.1.0")
    assert list_deployed_releases(s) == ["1.0.0", "1.1.0"]


def test_refuse_current_symlink_pointing_to_nested_directory(tmp_path: Path):
    s = spec(tmp_path)
    nested = s.root / ".staging" / "1.0.0"
    nested.mkdir(parents=True)
    s.current_link.symlink_to(nested)
    assert current_version(s) is None
    with pytest.raises(ReleaseError, match="invalid current symlink"):
        validate_managed_links(s)
    with pytest.raises(ReleaseError, match="invalid current symlink"):
        activate(s, "1.1.0")
    assert s.current_link.resolve() == nested


def test_refuse_previous_symlink_pointing_outside_root(tmp_path: Path):
    s = spec(tmp_path)
    (s.root / "current").symlink_to(s.root / "1.0.0")
    outside = tmp_path / "external" / "1.1.0"
    outside.mkdir(parents=True)
    s.previous_link.symlink_to(outside)
    with pytest.raises(ReleaseError, match="invalid previous symlink"):
        activate(s, "1.1.0")
    assert current_version(s) == "1.0.0"


def test_refuse_release_directory_symlink(tmp_path: Path):
    s = spec(tmp_path)
    (s.root / "1.2.0").symlink_to(s.root / "1.0.0", target_is_directory=True)
    with pytest.raises(ReleaseError, match="not deployed"):
        ensure_release_exists(s, "1.2.0")
    assert "1.2.0" not in list_deployed_releases(s)


@pytest.mark.parametrize("reserved", ["current", "previous"])
def test_refuse_reserved_version_directories(tmp_path: Path, reserved: str):
    s = spec(tmp_path)
    with pytest.raises(ReleaseError, match="invalid managed release version"):
        release_path(s, reserved)
