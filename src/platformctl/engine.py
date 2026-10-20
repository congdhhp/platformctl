from __future__ import annotations

from dataclasses import dataclass

from .errors import ApplyError, ReleaseError
from .models import ServiceSpec, StackManifest
from .release import activate, current_version, ensure_release_exists, rollback_link
from .systemd import Systemd


@dataclass(frozen=True)
class ServiceStatus:
    name: str
    desired: str
    current: str | None
    state: str
    enabled: bool
    mode: str = "managed"


class Engine:
    def __init__(self, systemd: Systemd | None = None) -> None:
        self.systemd = systemd or Systemd()

    def validate(self, manifest: StackManifest) -> None:
        for spec in manifest.services.values():
            if spec.managed:
                ensure_release_exists(spec)
            elif spec.install_path is None or not spec.install_path.is_dir():
                raise ReleaseError(f"installation directory not found: {spec.install_path}")

    def status(self, manifest: StackManifest) -> list[ServiceStatus]:
        rows: list[ServiceStatus] = []
        for spec in manifest.services.values():
            rows.append(
                ServiceStatus(
                    name=spec.name,
                    desired=spec.version,
                    current=current_version(spec),
                    state="running" if self.systemd.is_active(spec.unit) else "stopped",
                    enabled=self.systemd.is_enabled(spec.unit),
                    mode="managed" if spec.managed else "external",
                )
            )
        return rows

    def _apply_external_service(self, spec: ServiceSpec) -> None:
        # install_path is an existing installation, not a release we can switch.
        # Its declared version is metadata; no version discovery or rollback.
        if spec.install_path is None or not spec.install_path.is_dir():
            raise ReleaseError(f"installation directory not found: {spec.install_path}")
        try:
            if spec.autostart:
                self.systemd.enable(spec.unit)
            else:
                self.systemd.disable(spec.unit)

            active = self.systemd.is_active(spec.unit)
            if spec.state == "running" and not active:
                self.systemd.start(spec.unit)
            elif spec.state == "stopped" and active:
                self.systemd.stop(spec.unit)

            if self.systemd.is_active(spec.unit) != (spec.state == "running"):
                raise ApplyError(
                    f"{spec.unit} did not reach desired state: {spec.state}"
                )
        except ApplyError:
            raise
        except Exception as exc:
            raise ApplyError(
                f"failed reconciling external installation {spec.name}: {exc}"
            ) from exc

    def apply_service(self, spec: ServiceSpec) -> tuple[str | None, str]:
        if not spec.managed:
            self._apply_external_service(spec)
            return None, spec.version

        ensure_release_exists(spec)
        old_version = current_version(spec)
        was_active = self.systemd.is_active(spec.unit)
        changed = old_version != spec.version

        if spec.state == "stopped":
            if was_active:
                self.systemd.stop(spec.unit)
            activate(spec)
            if spec.autostart:
                self.systemd.enable(spec.unit)
            else:
                self.systemd.disable(spec.unit)
            return old_version, spec.version

        if changed and was_active:
            self.systemd.stop(spec.unit)

        try:
            activate(spec)
            if spec.autostart:
                self.systemd.enable(spec.unit)
            else:
                self.systemd.disable(spec.unit)
            if changed or not was_active:
                self.systemd.start(spec.unit)
        except Exception as exc:
            if changed and old_version is not None:
                try:
                    rollback_link(spec)
                    if was_active:
                        self.systemd.start(spec.unit)
                except Exception as rollback_exc:
                    raise ApplyError(
                        f"failed applying {spec.name}: {exc}; rollback also failed: {rollback_exc}"
                    ) from exc
            raise ApplyError(f"failed applying {spec.name}: {exc}") from exc

        if not self.systemd.is_active(spec.unit):
            if changed and old_version is not None:
                try:
                    self.systemd.stop(spec.unit)
                    rollback_link(spec)
                    self.systemd.start(spec.unit)
                except Exception as rollback_exc:
                    raise ApplyError(
                        f"{spec.unit} did not become active; rollback failed: {rollback_exc}"
                    )
            raise ApplyError(f"{spec.unit} did not become active")

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
        was_active = self.systemd.is_active(spec.unit)
        if was_active:
            self.systemd.stop(spec.unit)
        try:
            old, new = rollback_link(spec)
            if spec.state == "running" or was_active:
                self.systemd.start(spec.unit)
        except Exception:
            if was_active:
                try:
                    self.systemd.start(spec.unit)
                except Exception:
                    pass
            raise
        return old, new
