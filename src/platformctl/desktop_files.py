from __future__ import annotations

import configparser
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .errors import DesktopError
from .models import ServiceSpec

_DESKTOP_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]*\.desktop$")


@dataclass(frozen=True)
class DesktopChange:
    destination: Path
    before: bytes | None
    after: bytes


class DesktopFileManager:
    """Register full XDG application launchers without starting GUI processes.

    /usr/local/share/applications has priority over vendor /usr/share/applications
    in the standard XDG data directory search order. The launcher should use
    <root>/current, so changing versions does not require rewriting it.
    """

    def __init__(self, desktop_dir: Path = Path("/usr/local/share/applications")) -> None:
        self.desktop_dir = Path(desktop_dir)

    @staticmethod
    def validate_source(spec: ServiceSpec) -> bytes:
        source = spec.desktop_file
        if source is None:
            raise DesktopError(f"{spec.name}: desktop_file is required for type: desktop")
        if not _DESKTOP_NAME.fullmatch(source.name):
            raise DesktopError(f"desktop_file must have a safe .desktop filename: {source}")
        if not source.is_file():
            raise DesktopError(f"desktop_file not found: {source}")
        try:
            content = source.read_bytes()
            decoded = content.decode("utf-8-sig")
        except (OSError, UnicodeError) as exc:
            raise DesktopError(f"cannot read desktop_file {source}: {exc}") from exc
        if "\x00" in decoded:
            raise DesktopError(f"desktop_file contains NUL bytes: {source}")
        parser = configparser.ConfigParser(
            interpolation=None, strict=True, comment_prefixes=("#",),
            inline_comment_prefixes=None,
        )
        parser.optionxform = str
        try:
            parser.read_string(decoded)
        except configparser.Error as exc:
            raise DesktopError(f"invalid desktop_file {source}: {exc}") from exc
        if not parser.has_section("Desktop Entry"):
            raise DesktopError(f"desktop_file missing [Desktop Entry]: {source}")
        entry = parser["Desktop Entry"]
        if entry.get("Type", "").strip() != "Application":
            raise DesktopError(f"desktop_file must set Type=Application: {source}")
        for key in ("Name", "Exec"):
            if not entry.get(key, "").strip():
                raise DesktopError(f"desktop_file missing {key}: {source}")
        if entry.get("Hidden", "").strip().lower() == "true":
            raise DesktopError(f"desktop_file is marked Hidden=true: {source}")
        return content

    def prepare(self, spec: ServiceSpec) -> DesktopChange | None:
        content = self.validate_source(spec)
        assert spec.desktop_file is not None
        if self.desktop_dir.is_symlink() or (
            self.desktop_dir.exists() and not self.desktop_dir.is_dir()
        ):
            raise DesktopError(
                f"desktop launcher directory must be a real directory: {self.desktop_dir}"
            )
        target = self.desktop_dir / spec.desktop_file.name
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise DesktopError(f"refusing to overwrite non-regular launcher: {target}")
        try:
            old = target.read_bytes() if target.exists() else None
        except OSError as exc:
            raise DesktopError(f"cannot read installed launcher {target}: {exc}") from exc
        if old == content:
            return None
        return DesktopChange(target, old, content)

    def status(self, spec: ServiceSpec) -> str:
        change = self.prepare(spec)
        return "registered" if change is None else (
            "missing" if change.before is None else "outdated"
        )

    @staticmethod
    def _write_atomic(target: Path, content: bytes) -> None:
        temp_name: str | None = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_symlink() or (target.exists() and not target.is_file()):
                raise DesktopError(f"refusing to overwrite non-regular launcher: {target}")
            with tempfile.NamedTemporaryFile(
                dir=target.parent, prefix=f".{target.name}.", delete=False
            ) as temp:
                temp_name = temp.name
                os.fchmod(temp.fileno(), 0o644)
                temp.write(content)
                temp.flush()
                os.fsync(temp.fileno())
            os.replace(temp_name, target)
        except OSError as exc:
            raise DesktopError(f"failed writing launcher {target}: {exc}") from exc
        finally:
            if temp_name is not None:
                Path(temp_name).unlink(missing_ok=True)

    def install(self, change: DesktopChange) -> None:
        self._write_atomic(change.destination, change.after)

    def restore(self, change: DesktopChange) -> None:
        if change.before is None:
            change.destination.unlink(missing_ok=True)
        else:
            self._write_atomic(change.destination, change.before)
