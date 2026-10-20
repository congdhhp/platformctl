from __future__ import annotations

import os
from pathlib import Path

from .errors import ReleaseError
from .models import ServiceSpec


def release_path(spec: ServiceSpec, version: str | None = None) -> Path:
    return spec.releases_dir / (version or spec.version)


def ensure_release_exists(spec: ServiceSpec, version: str | None = None) -> Path:
    if not spec.managed:
        raise ReleaseError(f"{spec.name} uses install_path; no managed releases available")
    path = release_path(spec, version)
    if not path.is_dir():
        raise ReleaseError(f"release not deployed: {path}")
    return path


def current_version(spec: ServiceSpec) -> str | None:
    if not spec.managed:
        return None  # External installation version cannot be verified from symlinks.
    link = spec.current_link
    if not link.is_symlink():
        return None
    try:
        target = link.resolve(strict=True)
    except FileNotFoundError:
        return None
    try:
        return target.relative_to(spec.releases_dir.resolve()).parts[0]
    except (ValueError, IndexError):
        return None


def previous_version(spec: ServiceSpec) -> str | None:
    if not spec.managed:
        return None
    link = spec.previous_link
    if not link.is_symlink():
        return None
    try:
        target = link.resolve(strict=True)
    except FileNotFoundError:
        return None
    try:
        return target.relative_to(spec.releases_dir.resolve()).parts[0]
    except (ValueError, IndexError):
        return None


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
    previous = previous_version(spec)
    if previous is None:
        raise ReleaseError(f"no previous release recorded for {spec.name}")
    old = current_version(spec)
    activate(spec, previous)
    return old, previous
