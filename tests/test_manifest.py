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
    assert spec.unit == "softA.service"
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
