from __future__ import annotations

import os
import re
from pathlib import Path

from .errors import ReleaseError
from .models import ServiceSpec

_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+~-]*$")
_RESERVED_NAMES = frozenset({"current", "previous", "releases"})


def is_version_name(name: str) -> bool:
    """Whether a directory name is eligible as a managed release."""
    return bool(_VERSION_RE.fullmatch(name)) and name not in _RESERVED_NAMES


def release_path(spec: ServiceSpec, version: str | None = None) -> Path:
    if not spec.managed:
        raise ReleaseError(f"{spec.name} uses install_path; no managed releases available")
    selected = version if version is not None else spec.version
    if not is_version_name(selected):
        raise ReleaseError(f"invalid managed release version: {selected!r}")
    return spec.version_root / selected


def ensure_release_exists(spec: ServiceSpec, version: str | None = None) -> Path:
    path = release_path(spec, version)
    if not path.is_dir() or path.is_symlink():
        raise ReleaseError(f"release not deployed as a directory: {path}")
    return path


def _linked_version(spec: ServiceSpec, link: Path) -> str | None:
    if not spec.managed or not link.is_symlink():
        return None
    try:
        target = link.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    root = spec.version_root.resolve()
    if (
        target.parent != root
        or not is_version_name(target.name)
        or not target.is_dir()
        or (root / target.name).is_symlink()
    ):
        return None
    return target.name


def current_version(spec: ServiceSpec) -> str | None:
    return _linked_version(spec, spec.current_link) if spec.managed else None


def previous_version(spec: ServiceSpec) -> str | None:
    return _linked_version(spec, spec.previous_link) if spec.managed else None


def validate_managed_links(spec: ServiceSpec) -> None:
    """Refuse to overwrite links from the previous releases/ layout or unsafe paths."""
    if not spec.managed:
        return
    for label, link in (
        ("current", spec.current_link),
        ("previous", spec.previous_link),
    ):
        if link.is_symlink():
            if _linked_version(spec, link) is None:
                raise ReleaseError(
                    f"invalid {label} symlink: {link}; "
                    "migrate the release layout and relink it before applying"
                )
        elif link.exists():
            raise ReleaseError(f"{label} must be a symlink: {link}")


def list_deployed_releases(spec: ServiceSpec) -> list[str]:
    if not spec.managed:
        raise ReleaseError(f"{spec.name} uses install_path; list-releases is unavailable")
    root = spec.version_root
    if not root.is_dir():
        raise ReleaseError(f"version root not found: {root}")
    return sorted(
        path.name
        for path in root.iterdir()
        if path.is_dir() and not path.is_symlink() and is_version_name(path.name)
    )


def _replace_symlink(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    temp = link.parent / f".{link.name}.tmp-{os.getpid()}"
    try:
        temp.unlink(missing_ok=True)
        temp.symlink_to(target)
        os.replace(temp, link)
    finally:
        temp.unlink(missing_ok=True)


def activate(spec: ServiceSpec, version: str | None = None) -> tuple[str | None, str]:
    target = ensure_release_exists(spec, version)
    validate_managed_links(spec)
    target_version = target.name
    old_version = current_version(spec)
    if old_version == target_version:
        return old_version, target_version

    if old_version is not None:
        old_target = ensure_release_exists(spec, old_version)
        _replace_symlink(spec.previous_link, old_target)

    _replace_symlink(spec.current_link, target)
    return old_version, target_version


def rollback_link(spec: ServiceSpec) -> tuple[str | None, str]:
    if not spec.managed:
        raise ReleaseError(f"{spec.name} uses install_path; rollback is unavailable")
    validate_managed_links(spec)
    previous = previous_version(spec)
    if previous is None:
        raise ReleaseError(f"no previous release recorded for {spec.name}")
    old = current_version(spec)
    activate(spec, previous)
    return old, previous
