from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

import yaml

from .errors import ManifestError
from .models import ServiceSpec, StackManifest
from .release import is_version_name

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]*$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+~-]*$")
_SERVICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@:+\\-]*\\.service$")
_TOP_LEVEL_KEYS = {"schema", "platform", "services"}
_PLATFORM_KEYS = {"root"}
_SERVICE_KEYS = {"version", "service_name", "service_file", "root", "install_path", "state", "autostart", "enabled"}


def _mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ManifestError(f"{where} must be a mapping")
    return value


def _reject_unknown(mapping: Mapping[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise ManifestError(f"{where} contains unknown field(s): {', '.join(unknown)}")


def _absolute_path(value: Any, where: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{where} must be a non-empty absolute path")
    path = Path(value)
    if not path.is_absolute():
        raise ManifestError(f"{where} must be absolute: {value}")
    return path


def _bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise ManifestError(f"{where} must be true or false")
    return value


def load_manifest(path: str | Path) -> StackManifest:
    manifest_path = Path(path)
    try:
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ManifestError(f"manifest not found: {manifest_path}") from exc
    except OSError as exc:
        raise ManifestError(f"cannot read manifest {manifest_path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ManifestError(f"invalid YAML in {manifest_path}: {exc}") from exc

    top = _mapping(raw, "manifest")
    _reject_unknown(top, _TOP_LEVEL_KEYS, "manifest")

    schema = top.get("schema")
    if schema != 1:
        raise ManifestError("manifest.schema must be 1")

    platform = _mapping(top.get("platform", {}), "platform")
    _reject_unknown(platform, _PLATFORM_KEYS, "platform")
    platform_root = _absolute_path(platform.get("root", "/opt/platform"), "platform.root")

    services_raw = _mapping(top.get("services"), "services")
    if not services_raw:
        raise ManifestError("services must contain at least one service")

    services: dict[str, ServiceSpec] = {}
    for name, raw_spec in services_raw.items():
        if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
            raise ManifestError(f"invalid service name: {name!r}")

        spec = _mapping(raw_spec, f"services.{name}")
        _reject_unknown(spec, _SERVICE_KEYS, f"services.{name}")

        version = spec.get("version")
        if not isinstance(version, str) or not _VERSION_RE.fullmatch(version):
            raise ManifestError(f"services.{name}.version is invalid: {version!r}")

        service_name = spec.get("service_name", f"{name}.service")
        if not isinstance(service_name, str) or not _SERVICE_RE.fullmatch(service_name):
            raise ManifestError(
                f"services.{name}.service_name is invalid: {service_name!r}"
            )

        service_file = None
        if "service_file" in spec:
            file_value = spec["service_file"]
            if not isinstance(file_value, str) or not file_value.strip():
                raise ManifestError(f"services.{name}.service_file must be a file path")
            candidate = Path(file_value)
            service_file = (
                candidate if candidate.is_absolute()
                else manifest_path.resolve().parent / candidate
            ).resolve()

        if "root" in spec and "install_path" in spec:
            raise ManifestError(
                f"services.{name}: root and install_path are mutually exclusive"
            )
        install_path = (
            _absolute_path(spec["install_path"], f"services.{name}.install_path")
            if "install_path" in spec
            else None
        )
        root = (
            _absolute_path(spec["root"], f"services.{name}.root")
            if "root" in spec
            else platform_root / name
        ) if install_path is None else None
        if install_path is None and not is_version_name(version):
            raise ManifestError(
                f"services.{name}.version conflicts with a managed directory name: {version!r}"
            )

        if "enabled" in spec:
            if "state" in spec or "autostart" in spec:
                raise ManifestError(
                    f"services.{name}.enabled cannot be combined with state/autostart"
                )
            enabled = _bool(spec["enabled"], f"services.{name}.enabled")
            state = "running" if enabled else "stopped"
            autostart = enabled
        else:
            state = spec.get("state", "running")
            if state not in {"running", "stopped"}:
                raise ManifestError(f"services.{name}.state must be 'running' or 'stopped'")
            autostart = _bool(spec.get("autostart", True), f"services.{name}.autostart")

        services[name] = ServiceSpec(
            name=name,
            version=version,
            service_name=service_name,
            service_file=service_file,
            root=root,
            install_path=install_path,
            state=state,
            autostart=autostart,
        )

    unit_owners: dict[str, str] = {}
    for name, definition in services.items():
        owner = unit_owners.setdefault(definition.service_name, name)
        if owner != name:
            raise ManifestError(
                f"services.{name} and services.{owner} use the same "
                f"service_name: {definition.service_name}"
            )

    return StackManifest(schema=schema, platform_root=platform_root, services=services)
