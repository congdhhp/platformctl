from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .errors import ReleaseError

ServiceState = Literal["running", "stopped"]


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    version: str
    service_name: str
    root: Path | None
    state: ServiceState = "running"
    autostart: bool = True
    install_path: Path | None = None
    service_file: Path | None = None
    source: str | None = None

    def __post_init__(self) -> None:
        if (self.root is None) == (self.install_path is None):
            raise ValueError("exactly one of root or install_path is required")

    @property
    def managed(self) -> bool:
        return self.install_path is None

    def _managed_root(self) -> Path:
        if self.root is None:
            raise ReleaseError(
                f"{self.name} uses install_path; managed release operations are unavailable"
            )
        return self.root

    @property
    def version_root(self) -> Path:
        """Direct parent of immutable version directories in managed mode."""
        return self._managed_root()

    @property
    def current_link(self) -> Path:
        return self._managed_root() / "current"

    @property
    def previous_link(self) -> Path:
        return self._managed_root() / "previous"


@dataclass(frozen=True)
class StackManifest:
    schema: int
    platform_root: Path
    services: dict[str, ServiceSpec]
    project_directory: Path | None = None
    global_source: str | None = None
