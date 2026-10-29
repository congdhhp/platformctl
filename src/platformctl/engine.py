from __future__ import annotations

from dataclasses import dataclass

from .errors import ApplyError, ReleaseError
from .desktop_files import DesktopFileManager
from .models import ServiceSpec, StackManifest
from .packages import PackageManager
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
    enabled: bool | None
    mode: str = "managed"
    type: str = "service"


class Engine:
    def __init__(
        self,
        systemd: Systemd | None = None,
        unit_files: UnitFileManager | None = None,
        packages: PackageManager | None = None,
        desktop_files: DesktopFileManager | None = None,
    ) -> None:
        self.systemd = systemd or Systemd()
        self.unit_files = unit_files or UnitFileManager()
        self.packages = packages or PackageManager()
        self.desktop_files = desktop_files or DesktopFileManager()

    def _validate_service(self, spec: ServiceSpec) -> None:
        if spec.managed:
            ensure_release_exists(spec)
            validate_managed_links(spec)
            if spec.source is not None:
                ready, detail = self.packages.check(spec)
                if not ready:
                    raise ReleaseError(
                        f"{spec.name} is not ready for activation: {detail}; "
                        "run platformctl install and platformctl check first"
                    )
        elif spec.install_path is None or not spec.install_path.is_dir():
            raise ReleaseError(f"installation directory not found: {spec.install_path}")
        if spec.type == "desktop":
            self.desktop_files.prepare(spec)
        else:
            self.unit_files.prepare(spec)

    def validate(self, manifest: StackManifest) -> None:
        """Validate configuration without requiring desired releases on the host."""
        for spec in manifest.services.values():
            if spec.type == "desktop":
                self.desktop_files.validate_source(spec)
            else:
                self.unit_files.validate_source(spec)

    def check(self, manifest: StackManifest) -> None:
        """Check that desired releases exist before any systemd changes."""
        for spec in manifest.services.values():
            self._validate_service(spec)

    def status(self, manifest: StackManifest) -> list[ServiceStatus]:
        rows: list[ServiceStatus] = []
        for spec in manifest.services.values():
            if spec.type == "desktop":
                rows.append(
                    ServiceStatus(
                        name=spec.name,
                        desired=spec.version,
                        current=current_version(spec),
                        state=self.desktop_files.status(spec),
                        enabled=None,
                        mode="managed" if spec.managed else "external",
                        type="desktop",
                    )
                )
            else:
                rows.append(
                    ServiceStatus(
                        name=spec.name,
                        desired=spec.version,
                        current=current_version(spec),
                        state="running" if self.systemd.is_active(spec.service_name) else "stopped",
                        enabled=self.systemd.is_enabled(spec.service_name),
                        mode="managed" if spec.managed else "external",
                        type="service",
                    )
                )
        return rows

    def _apply_desktop(self, spec: ServiceSpec) -> tuple[str | None, str]:
        """Select release and register its launcher; do not touch a GUI session."""
        self._validate_service(spec)
        change = self.desktop_files.prepare(spec)
        previous = current_version(spec)
        links_before = snapshot_links(spec)
        touched_links = False
        touched_desktop = False
        try:
            touched_links = True
            activate(spec)
            if change is not None:
                touched_desktop = True
                self.desktop_files.install(change)
            if self.desktop_files.status(spec) != "registered":
                raise ApplyError(f"{spec.name}: desktop launcher was not registered")
        except Exception as exc:
            problems: list[str] = []
            if touched_desktop and change is not None:
                try:
                    self.desktop_files.restore(change)
                except Exception as restore_exc:
                    problems.append(f"restore desktop launcher: {restore_exc}")
            if touched_links:
                try:
                    restore_links(spec, links_before)
                except Exception as restore_exc:
                    problems.append(f"restore release links: {restore_exc}")
            message = f"failed applying desktop {spec.name}: {exc}"
            if problems:
                message += "; rollback also failed: " + "; ".join(problems)
            raise ApplyError(message) from exc
        return previous, spec.version

    def apply_service(self, spec: ServiceSpec) -> tuple[str | None, str]:
        if spec.type == "desktop":
            return self._apply_desktop(spec)
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
        self.check(manifest)
        changes: list[tuple[str, str | None, str]] = []
        for spec in manifest.services.values():
            old, new = self.apply_service(spec)
            changes.append((spec.name, old, new))
        return changes

    def rollback(self, spec: ServiceSpec) -> tuple[str | None, str]:
        if not spec.managed:
            raise ReleaseError(f"{spec.name} uses install_path; rollback is unavailable")
        validate_managed_links(spec)
        if spec.type == "desktop":
            # The launcher references <root>/current and is not rewritten on
            # rollback. Existing GUI processes are intentionally left alone.
            return rollback_link(spec)
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
