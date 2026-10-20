from __future__ import annotations

from dataclasses import dataclass

from .errors import ApplyError, ReleaseError
from .models import ServiceSpec, StackManifest
from .release import (
    activate,
    current_version,
    ensure_release_exists,
    restore_links,
    rollback_link,
    snapshot_links,
    validate_managed_links,
)
from .systemd import Systemd
from .unit_files import UnitFileManager


@dataclass(frozen=True)
class ServiceStatus:
    name: str
    desired: str
    current: str | None
    state: str
    enabled: bool
    mode: str = "managed"


class Engine:
    def __init__(
        self,
        systemd: Systemd | None = None,
        unit_files: UnitFileManager | None = None,
    ) -> None:
        self.systemd = systemd or Systemd()
        self.unit_files = unit_files or UnitFileManager()

    def _validate_service(self, spec: ServiceSpec) -> None:
        if spec.managed:
            ensure_release_exists(spec)
            validate_managed_links(spec)
        elif spec.install_path is None or not spec.install_path.is_dir():
            raise ReleaseError(f"installation directory not found: {spec.install_path}")
        self.unit_files.prepare(spec)

    def validate(self, manifest: StackManifest) -> None:
        for spec in manifest.services.values():
            self._validate_service(spec)

    def status(self, manifest: StackManifest) -> list[ServiceStatus]:
        rows: list[ServiceStatus] = []
        for spec in manifest.services.values():
            rows.append(
                ServiceStatus(
                    name=spec.name,
                    desired=spec.version,
                    current=current_version(spec),
                    state="running" if self.systemd.is_active(spec.service_name) else "stopped",
                    enabled=self.systemd.is_enabled(spec.service_name),
                    mode="managed" if spec.managed else "external",
                )
            )
        return rows

    def apply_service(self, spec: ServiceSpec) -> tuple[str | None, str]:
        # Finish preflight before stopping or changing any running service.
        self._validate_service(spec)
        unit_change = self.unit_files.prepare(spec)

        old_version = current_version(spec)
        link_snapshot = snapshot_links(spec) if spec.managed else None
        was_active = self.systemd.is_active(spec.service_name)
        was_enabled = self.systemd.is_enabled(spec.service_name)
        version_changed = spec.managed and old_version != spec.version
        needs_restart = version_changed or unit_change is not None
        touched_unit = False
        touched_links = False

        try:
            if was_active and (spec.state == "stopped" or needs_restart):
                self.systemd.stop(spec.service_name)

            if unit_change is not None:
                touched_unit = True
                self.unit_files.install(unit_change)
                self.systemd.daemon_reload()

            if spec.managed:
                touched_links = True
                activate(spec)

            if spec.autostart:
                self.systemd.enable(spec.service_name)
            else:
                self.systemd.disable(spec.service_name)

            if spec.state == "running" and (needs_restart or not was_active):
                self.systemd.start(spec.service_name)

            if self.systemd.is_active(spec.service_name) != (spec.state == "running"):
                raise ApplyError(
                    f"{spec.service_name} did not reach desired state: {spec.state}"
                )

        except Exception as exc:
            errors: list[str] = []
            try:
                if self.systemd.is_active(spec.service_name):
                    self.systemd.stop(spec.service_name)
            except Exception as undo_exc:
                errors.append(f"stop: {undo_exc}")

            if touched_links and link_snapshot is not None:
                try:
                    restore_links(spec, link_snapshot)
                except Exception as undo_exc:
                    errors.append(f"restore links: {undo_exc}")

            if touched_unit and unit_change is not None:
                try:
                    self.unit_files.restore(unit_change)
                    self.systemd.daemon_reload()
                except Exception as undo_exc:
                    errors.append(f"restore unit: {undo_exc}")

            try:
                if was_enabled:
                    self.systemd.enable(spec.service_name)
                else:
                    self.systemd.disable(spec.service_name)
            except Exception as undo_exc:
                errors.append(f"restore autostart: {undo_exc}")

            if was_active:
                try:
                    self.systemd.start(spec.service_name)
                except Exception as undo_exc:
                    errors.append(f"restore runtime: {undo_exc}")

            detail = f"failed applying {spec.name}: {exc}"
            if errors:
                detail += "; rollback also failed: " + "; ".join(errors)
            raise ApplyError(detail) from exc

        return old_version, spec.version

    def apply(self, manifest: StackManifest) -> list[tuple[str, str | None, str]]:
        self.validate(manifest)
        changes: list[tuple[str, str | None, str]] = []
        for spec in manifest.services.values():
            old, new = self.apply_service(spec)
            changes.append((spec.name, old, new))
        return changes

    def rollback(self, spec: ServiceSpec) -> tuple[str | None, str]:
        if not spec.managed:
            raise ReleaseError(f"{spec.name} uses install_path; rollback is unavailable")
        validate_managed_links(spec)
        was_active = self.systemd.is_active(spec.service_name)
        if was_active:
            self.systemd.stop(spec.service_name)
        try:
            old, new = rollback_link(spec)
            if spec.state == "running" or was_active:
                self.systemd.start(spec.service_name)
        except Exception:
            if was_active:
                try:
                    self.systemd.start(spec.service_name)
                except Exception:
                    pass
            raise
        return old, new
