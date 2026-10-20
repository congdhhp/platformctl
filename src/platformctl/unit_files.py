from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .errors import SystemdError
from .models import ServiceSpec


@dataclass(frozen=True)
class UnitChange:
    destination: Path
    before: bytes | None
    after: bytes


class UnitFileManager:
    """Install full systemd unit overrides; leave unconfigured units untouched."""

    def __init__(self, unit_dir: Path = Path("/etc/systemd/system")) -> None:
        self.unit_dir = Path(unit_dir)

    def prepare(self, spec: ServiceSpec) -> UnitChange | None:
        if spec.service_file is None:
            return None

        source = spec.service_file
        if not source.is_file():
            raise SystemdError(f"service_file does not exist: {source}")
        try:
            content = source.read_bytes()
            decoded = content.decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise SystemdError(f"cannot read service_file {source}: {exc}") from exc
        if not re.search(r"(?m)^\s*\[Service\]\s*$", decoded):
            raise SystemdError(f"service_file is missing [Service] section: {source}")
        if not re.search(r"(?m)^\s*ExecStart\s*=", decoded):
            raise SystemdError(f"service_file is missing ExecStart: {source}")

        if not self.unit_dir.is_dir():
            raise SystemdError(f"systemd unit directory does not exist: {self.unit_dir}")

        target = self.unit_dir / spec.service_name
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise SystemdError(f"refusing to overwrite non-regular unit: {target}")
        try:
            old = target.read_bytes() if target.exists() else None
        except OSError as exc:
            raise SystemdError(f"cannot read installed unit {target}: {exc}") from exc
        if old == content:
            return None
        return UnitChange(destination=target, before=old, after=content)

    @staticmethod
    def _write_atomic(target: Path, content: bytes) -> None:
        tmp_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix=f".{target.name}.",
                                             delete=False) as tmp:
                tmp_name = tmp.name
                os.fchmod(tmp.fileno(), 0o644)
                tmp.write(content)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_name, target)
        except OSError as exc:
            raise SystemdError(f"failed writing systemd unit {target}: {exc}") from exc
        finally:
            if tmp_name is not None:
                Path(tmp_name).unlink(missing_ok=True)

    def install(self, change: UnitChange) -> None:
        self._write_atomic(change.destination, change.after)

    def restore(self, change: UnitChange) -> None:
        if change.before is None:
            change.destination.unlink(missing_ok=True)
        else:
            self._write_atomic(change.destination, change.before)
