"""Controller-only agentless orchestration over OpenSSH.

Remote Linux nodes require Bash, dpkg, systemctl (for service targets) and
coreutils, but neither Python nor a platformctl installation. The Bash worker
is streamed over stdin for each operation and is never left on target disks.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import os
import re
import shlex
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from .artifacts import artifact_for
from .desktop_files import DesktopFileManager
from .errors import OrchestrationError
from .inventory import Host
from .models import ServiceSpec, StackManifest
from .packages import PackageManager, expected_package_name
from .planner import Target
from .remote_worker import WORKER
from .unit_files import UnitFileManager

_WORKSPACE = re.compile(r"^/tmp/platformctl-[A-Za-z0-9]+$")
_MUTATING = {"install", "apply", "rollback"}
_COMMANDS = {"status", "diff", "check", "install", "apply", "rollback", "list-releases"}
_MAX_ARTIFACT_BYTES = 8 * 1024 * 1024 * 1024


def _sha256(path: Path) -> str:
    """Streaming checksum compatible with Python 3.10 and newer."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class HostResult:
    host: str
    command: str
    code: int
    output: str
    error: str = ""


class SSHTransport:
    """Non-interactive OpenSSH; never interpolate config values into shell code."""

    def _ssh(self, host: Host) -> list[str]:
        assert host.connection == "ssh" and host.address
        args = [
            "ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", "ConnectTimeout=10", "-p", str(host.port),
        ]
        if host.user:
            args += ["-l", host.user]
        return args + [host.address]

    def run(
        self, host: Host, argv: list[str], file: Path | None = None,
        *, stdin: bytes | None = None, timeout: int = 600,
    ) -> HostResult:
        if file is not None and stdin is not None:
            raise OrchestrationError("file and stdin cannot be supplied together")
        command = self._ssh(host) + [shlex.join(argv)]
        try:
            if file is not None:
                with file.open("rb") as input_file:
                    process = subprocess.run(
                        command, stdin=input_file, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, check=False, timeout=timeout,
                    )
            else:
                process = subprocess.run(
                    command, input=stdin, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, check=False, timeout=timeout,
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise OrchestrationError(f"{host.name}: SSH command failed: {exc}") from exc
        return HostResult(
            host.name, argv[0], process.returncode,
            process.stdout.decode(errors="replace"),
            process.stderr.decode(errors="replace"),
        )

    def require(
        self, host: Host, argv: list[str], file: Path | None = None,
        *, stdin: bytes | None = None, timeout: int = 600,
    ) -> str:
        result = self.run(host, argv, file, stdin=stdin, timeout=timeout)
        if result.code:
            raise OrchestrationError(
                f"{host.name}: remote command failed ({result.code}): "
                f"{result.error.strip() or result.output.strip()}"
            )
        return result.output.strip()

    def workspace(self, host: Host) -> str:
        path = self.require(host, ["mktemp", "-d", "/tmp/platformctl-XXXXXXXX"])
        if not _WORKSPACE.fullmatch(path):
            raise OrchestrationError(
                f"{host.name}: unsafe remote staging directory: {path!r}"
            )
        return path

    def put(self, host: Host, file: Path, destination: str) -> None:
        if not file.is_file() or file.is_symlink():
            raise OrchestrationError(f"controller file is missing or unsafe: {file}")
        # Private mktemp workspace is owned by the SSH user (mode 0700).
        self.require(
            host,
            ["sh", "-c", 'umask 077; cat > "$1"', "sh", destination],
            file=file, timeout=3600,
        )
        expected = _sha256(file)
        checksum = self.require(host, ["sha256sum", "--", destination]).split()[0]
        if checksum != expected:
            raise OrchestrationError(
                f"{host.name}: checksum mismatch after uploading {file.name}"
            )

    def get(self, host: Host, source: str, destination: Path) -> None:
        """Stream a target-local artifact to the controller for secure inspection."""
        command = self._ssh(host) + [shlex.join(["cat", "--", source])]
        try:
            with destination.open("wb") as output:
                process = subprocess.run(
                    command, stdout=output, stderr=subprocess.PIPE,
                    check=False, timeout=3600,
                )
            if process.returncode:
                raise OrchestrationError(
                    f"{host.name}: reading remote artifact failed: "
                    f"{process.stderr.decode(errors='replace').strip()}"
                )
            if destination.stat().st_size > _MAX_ARTIFACT_BYTES:
                raise OrchestrationError("remote artifact exceeds maximum size")
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise OrchestrationError(
                f"{host.name}: unable to retrieve remote artifact: {exc}"
            ) from exc

    def download(self, host: Host, url: str, destination: str) -> None:
        # No shell-expanded URL; argv is escaped by shlex.join in run().
        self.require(
            host,
            [
                "sh", "-c",
                'curl -fLsS --proto "=http,https" --proto-redir "=http,https" '
                '--max-time 1800 --output "$1" "$2"',
                "sh", destination, url,
            ],
            timeout=1900,
        )

    def execute(
        self, host: Host, argv: list[str], *, privileged: bool = False
    ) -> HostResult:
        command = (["sudo", "-n"] if privileged and host.become else [])
        command += ["bash", "-s", "--", *argv]
        return self.run(
            host, command, stdin=WORKER.encode("utf-8"), timeout=3600
        )

    def cleanup(self, host: Host, workspace: str) -> None:
        if not _WORKSPACE.fullmatch(workspace):
            raise OrchestrationError("unsafe remote staging path")
        self.require(host, ["rm", "-rf", "--", workspace])


def _local_command(
    host: Host, command: str, manifest: Path, software: str | None
) -> HostResult:
    argv = [command, "-f", str(manifest)]
    if software:
        argv.insert(1, software)
    if command in _MUTATING and host.become and os.geteuid() != 0:
        executable = (
            [sys.executable] if getattr(sys, "frozen", False)
            else [sys.executable, "-m", "platformctl.cli"]
        )
        try:
            result = subprocess.run(
                ["sudo", "-n", *executable, *argv],
                capture_output=True, text=True, check=False, timeout=3600,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise OrchestrationError(
                f"{host.name}: local sudo execution failed: {exc}"
            ) from exc
        return HostResult(
            host.name, command, result.returncode, result.stdout, result.stderr
        )

    from .cli import main
    output, errors = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
        code = main(argv)
    return HostResult(host.name, command, code, output.getvalue(), errors.getvalue())


def _local_manifest(stack: StackManifest, target: Target, folder: Path) -> Path:
    """Local controller calls existing Engine, with original resolved paths."""
    import yaml
    specs = {}
    for spec in target.software:
        obj = {"version": spec.version, "type": spec.type}
        if spec.managed:
            obj["root"] = str(spec.root)
        else:
            obj["install_path"] = str(spec.install_path)
        if spec.source is not None:
            obj["source"] = spec.source
        if spec.type == "desktop":
            obj["desktop_file"] = str(spec.desktop_file)
        else:
            obj.update(
                service_name=spec.service_name,
                state=spec.state,
                autostart=spec.autostart,
            )
            if spec.service_file:
                obj["service_file"] = str(spec.service_file)
        specs[spec.name] = obj
    path = folder / "stack.yaml"
    path.write_text(
        yaml.safe_dump({
            "schema": stack.schema,
            "platform": {"root": str(stack.platform_root)},
            "services": specs,
        }),
        encoding="utf-8",
    )
    return path


class Orchestrator:
    def __init__(self, transport: SSHTransport | None = None) -> None:
        self.transport = transport or SSHTransport()
        self.packages = PackageManager()

    def _artifact(
        self, host: Host, spec: ServiceSpec, staging: str, scratch: Path
    ) -> tuple[str, str]:
        location = artifact_for(spec)
        mode = spec.source_location or (
            "target" if location.remote else "controller"
        )
        dest = f"{staging}/{spec.name}.deb"
        local_copy = scratch / f"{spec.name}.deb"
        if mode == "controller":
            if location.remote:
                self.packages._download(location.location, local_copy)
            else:
                local_copy = Path(location.location)
            self.transport.put(host, local_copy, dest)
        elif location.remote:
            self.transport.download(host, location.location, dest)
            self.transport.get(host, dest, local_copy)
        else:
            # Existing target-local path: inspect without overwriting it.
            dest = location.location
            self.transport.get(host, dest, local_copy)

        size = local_copy.stat().st_size
        if size > _MAX_ARTIFACT_BYTES:
            raise OrchestrationError(f"{spec.name}: artifact exceeds maximum size")
        arch = self.transport.require(host, ["dpkg", "--print-architecture"])
        with tempfile.TemporaryDirectory(prefix="platformctl-inspect-") as directory:
            self.packages._inspect(spec, local_copy, Path(directory), target_arch=arch)
        digest = _sha256(local_copy)
        return dest, digest

    def _remote_one(
        self, host: Host, spec: ServiceSpec, command: str, workspace: str,
        scratch: Path,
    ) -> HostResult:
        unit_file = ""
        desktop_file = ""
        if spec.type == "desktop":
            DesktopFileManager.validate_source(spec)
            assert spec.desktop_file is not None
            desktop_file = f"{workspace}/{spec.desktop_file.name}"
            self.transport.put(host, spec.desktop_file, desktop_file)
        elif spec.service_file:
            UnitFileManager.validate_source(spec)
            unit_file = f"{workspace}/{spec.name}.service"
            self.transport.put(host, spec.service_file, unit_file)

        package = expected_package_name(spec) if spec.source is not None else ""
        deb = ""
        digest = ""
        if command == "install" and spec.source is not None:
            # Do not fetch a huge artifact if the desired release is already ready.
            preliminary = self._worker(
                host, spec, "check", unit_file, desktop_file, "", package
            )
            if preliminary.code == 0:
                return HostResult(host.name, "install", 0,
                                  f"{spec.name}: already installed\n")
            deb, digest = self._artifact(host, spec, workspace, scratch)
        return self._worker(
            host, spec, command, unit_file, desktop_file, deb, package, digest
        )

    def _worker(
        self, host: Host, spec: ServiceSpec, command: str,
        unit_file: str, desktop_file: str, deb: str, package: str,
        digest: str = "",
    ) -> HostResult:
        argv = [
            command, spec.name, spec.version, spec.type,
            str(spec.root if spec.managed else spec.install_path),
            "1" if spec.managed else "0",
            spec.service_name, spec.state,
            "true" if spec.autostart else "false",
            unit_file, desktop_file, deb, package,
            "1" if spec.source is not None else "0", digest,
        ]
        return self.transport.execute(
            host, argv, privileged=command in _MUTATING
        )

    def _host(
        self, stack: StackManifest, target: Target,
        command: str, software: str | None = None,
    ) -> HostResult:
        host = target.host
        host.verify_local()
        if command not in _COMMANDS:
            raise OrchestrationError(f"unsupported command: {command}")
        with tempfile.TemporaryDirectory(prefix="platformctl-controller-") as directory:
            scratch = Path(directory)
            if host.connection == "local":
                filename = _local_manifest(stack, target, scratch)
                return _local_command(host, command, filename, software)

            workspace = self.transport.workspace(host)
            try:
                outputs: list[str] = []
                for spec in target.software:
                    if software and spec.name != software:
                        continue
                    result = self._remote_one(
                        host, spec, command, workspace, scratch
                    )
                    if result.output.strip():
                        outputs.append(result.output.rstrip())
                    if result.code:
                        return HostResult(
                            host.name, command, result.code,
                            "\n".join(outputs), result.error,
                        )
                return HostResult(host.name, command, 0, "\n".join(outputs))
            finally:
                self.transport.cleanup(host, workspace)

    def _privilege_check(self, target: Target) -> HostResult:
        host = target.host
        if not host.become:
            return HostResult(host.name, "sudo-preflight", 0, "no sudo requested")
        if host.connection == "local" and os.geteuid() == 0:
            return HostResult(host.name, "sudo-preflight", 0, "already root")
        if host.connection == "ssh":
            check = self.transport.run(
                host, ["sudo", "-n", "bash", "-s", "--"], stdin=b"true\n"
            )
        else:
            try:
                process = subprocess.run(
                    ["sudo", "-n", "true"], capture_output=True,
                    text=True, check=False, timeout=30,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise OrchestrationError(
                    f"{host.name}: sudo preflight failed: {exc}"
                ) from exc
            check = HostResult(
                host.name, "sudo-preflight", process.returncode,
                process.stdout, process.stderr,
            )
        if check.code:
            raise OrchestrationError(
                f"{host.name}: privileged execution unavailable: "
                f"{check.error.strip() or check.output.strip()}"
            )
        return HostResult(host.name, "sudo-preflight", 0, "OK")

    def execute(
        self, stack: StackManifest, targets: tuple[Target, ...], command: str,
        *, software: str | None = None, parallel: int = 1,
    ) -> list[HostResult]:
        if parallel < 1 or parallel > 32:
            raise OrchestrationError("--parallel must be between 1 and 32")
        if command == "apply":
            checks = self.execute(stack, targets, "check", parallel=parallel)
            if not any(r.code for r in checks):
                def privilege(target: Target) -> HostResult:
                    try:
                        return self._privilege_check(target)
                    except Exception as exc:
                        return HostResult(target.host.name, "sudo-preflight", 2, "", str(exc))
                checks = [privilege(t) for t in targets]
            if any(r.code for r in checks):
                return [
                    HostResult(
                        r.host, "apply", 2, "",
                        ("preflight failed: " + (r.error or r.output).strip())
                        if r.code else "activation aborted: another host failed preflight"
                    ) for r in checks
                ]

        def run(target: Target) -> HostResult:
            try:
                return self._host(stack, target, command, software)
            except Exception as exc:
                return HostResult(target.host.name, command, 2, "", str(exc))
        if parallel == 1:
            return [run(t) for t in targets]
        # Local output capture redirects process-global stdout/stderr.
        # Execute local operations before starting SSH worker threads.
        result: dict[str, HostResult] = {}
        for target in targets:
            if target.host.connection == "local":
                result[target.host.name] = run(target)
        remotes = [t for t in targets if t.host.connection == "ssh"]
        with ThreadPoolExecutor(max_workers=parallel) as pool:
            result.update((r.host, r) for r in pool.map(run, remotes))
        return [result[t.host.name] for t in targets]
