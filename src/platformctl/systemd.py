from __future__ import annotations

import subprocess
from dataclasses import dataclass

from .errors import SystemdError


@dataclass
class Systemd:
    executable: str = "systemctl"

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            proc = subprocess.run(
                [self.executable, *args],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        except OSError as exc:
            raise SystemdError(f"cannot execute {self.executable}: {exc}") from exc
        if check and proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()
            raise SystemdError(f"systemctl {' '.join(args)} failed: {detail or proc.returncode}")
        return proc

    def is_active(self, unit: str) -> bool:
        return self._run("is-active", "--quiet", unit, check=False).returncode == 0

    def is_enabled(self, unit: str) -> bool:
        return self._run("is-enabled", "--quiet", unit, check=False).returncode == 0

    def start(self, unit: str) -> None:
        self._run("start", unit)

    def stop(self, unit: str) -> None:
        self._run("stop", unit)

    def restart(self, unit: str) -> None:
        self._run("restart", unit)

    def enable(self, unit: str) -> None:
        self._run("enable", unit)

    def disable(self, unit: str) -> None:
        self._run("disable", unit)
