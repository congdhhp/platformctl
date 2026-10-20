from pathlib import Path

import pytest

from platformctl.errors import ManifestError
from platformctl.manifest import load_manifest


def test_load_manifest(tmp_path: Path):
    p = tmp_path / "stack.yaml"
    p.write_text(
        """schema: 1
platform:
  root: /opt/platform
services:
  softA:
    version: 2.5.5-build184
""",
        encoding="utf-8",
    )
    manifest = load_manifest(p)
    spec = manifest.services["softA"]
    assert spec.root == Path("/opt/platform/softA")
    assert spec.service_name == "softA.service"
    assert spec.service_file is None
    assert spec.state == "running"
    assert spec.autostart is True


def test_reject_bad_version(tmp_path: Path):
    p = tmp_path / "stack.yaml"
    p.write_text(
        """schema: 1
services:
  softA:
    version: ../../etc/passwd
""",
        encoding="utf-8",
    )
    with pytest.raises(ManifestError):
        load_manifest(p)

def test_install_path_mode(tmp_path: Path):
    p = tmp_path / "stack.yaml"
    p.write_text(
        """schema: 1
services:
  legacy:
    version: 3.1.0
    install_path: /opt/vendor/legacy
""",
        encoding="utf-8",
    )
    spec = load_manifest(p).services["legacy"]
    assert spec.install_path == Path("/opt/vendor/legacy")
    assert spec.root is None
    assert not spec.managed


def test_managed_root_override(tmp_path: Path):
    p = tmp_path / "stack.yaml"
    p.write_text(
        """schema: 1
services:
  softA:
    version: 1.0.0
    root: /srv/native/softA
""",
        encoding="utf-8",
    )
    spec = load_manifest(p).services["softA"]
    assert spec.managed
    assert spec.root == Path("/srv/native/softA")
    assert spec.install_path is None


@pytest.mark.parametrize(
    "extra",
    [
        "root: /opt/softA\n    install_path: /usr/local/softA",
        "install_path: relative/path",
        "install_path: ''",
        "install_path: null",
    ],
)
def test_reject_invalid_install_path(tmp_path: Path, extra: str):
    p = tmp_path / "stack.yaml"
    p.write_text(
        "schema: 1\nservices:\n  softA:\n    version: 1.0.0\n    "
        + extra
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ManifestError):
        load_manifest(p)


@pytest.mark.parametrize("reserved", ["current", "previous"])
def test_reject_reserved_managed_versions(tmp_path: Path, reserved: str):
    p = tmp_path / "stack.yaml"
    p.write_text(
        f"schema: 1\nservices:\n  softA:\n    version: {reserved}\n",
        encoding="utf-8",
    )
    with pytest.raises(ManifestError, match="conflicts with a managed directory name"):
        load_manifest(p)


def test_service_file_relative_to_manifest_and_custom_name(tmp_path: Path):
    service_dir = tmp_path / "services"
    service_dir.mkdir()
    service_path = service_dir / "controller.service"
    service_path.write_text("[Service]\nExecStart=/opt/controller/current/run.sh\n")
    manifest_file = tmp_path / "stack.yaml"
    manifest_file.write_text(
        "schema: 1\nservices:\n  controller:\n"
        "    version: 2.5.5\n"
        "    service_name: controller-daemon.service\n"
        "    service_file: ./services/controller.service\n",
        encoding="utf-8",
    )
    spec = load_manifest(manifest_file).services["controller"]
    assert spec.service_name == "controller-daemon.service"
    assert spec.service_file == service_path


@pytest.mark.parametrize(
    "field",
    [
        "unit: softA.service",
        "service_name: ../bad.service",
        "service_name: softA.timer",
        "service_name: ''",
        "service_file: ''",
        "service_file: null",
    ],
)
def test_reject_invalid_systemd_fields(tmp_path: Path, field: str):
    manifest_file = tmp_path / "stack.yaml"
    manifest_file.write_text(
        "schema: 1\nservices:\n  softA:\n    version: 1.0.0\n"
        f"    {field}\n",
        encoding="utf-8",
    )
    with pytest.raises(ManifestError):
        load_manifest(manifest_file)


def test_reject_duplicate_service_names(tmp_path: Path):
    manifest_file = tmp_path / "stack.yaml"
    manifest_file.write_text(
        "schema: 1\nservices:\n"
        "  softA:\n    version: 1.0.0\n    service_name: shared.service\n"
        "  softB:\n    version: 1.0.0\n    service_name: shared.service\n",
        encoding="utf-8",
    )
    with pytest.raises(ManifestError, match="same service_name"):
        load_manifest(manifest_file)
