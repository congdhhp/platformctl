from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

ServiceState = Literal["running", "stopped"]


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    version: str
    unit: str
    root: Path
    state: ServiceState = "running"
    autostart: bool = True

    @property
    def releases_dir(self) -> Path:
        return self.root / "releases"

    @property
    def current_link(self) -> Path:
        return self.root / "current"

    @property
    def previous_link(self) -> Path:
        return self.root / "previous"


@dataclass(frozen=True)
class StackManifest:
    schema: int
    platform_root: Path
    services: dict[str, ServiceSpec]
