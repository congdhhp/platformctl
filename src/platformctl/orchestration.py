"""Agentless multi-host orchestration, reusing the existing local CLI/Engine.

The controller never performs remote dpkg/systemd changes via ad-hoc shell:
it securely stages a single-host manifest, then runs the matching platformctl
version on the target. OpenSSH handles host-key and SSH-agent authentication.
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

import yaml

from . import __version__
from .artifacts import artifact_for
from .errors import OrchestrationError
from .inventory import Host
from .models import ServiceSpec, StackManifest
from .packages import PackageManager
from .planner import Target

_WORKSPACE = re.compile(r"^/tmp/platformctl-[A-Za-z0-9]+$")
_MUTATING = {"install", "apply", "rollback"}
_COMMANDS = {"validate", "status", "diff", "check", "install", "apply", "rollback", "list-releases"}


@dataclass(frozen=True)
class HostResult:
    host: str
    command: str
    code: int
    output: str
    error: str = ""


class SSHTransport:
    """Strict, non-interactive OpenSSH transport; arguments never enter shell unquoted."""

    def _ssh(self, host: Host) -> list[str]:
        assert host.connection == "ssh" and host.address is not None
        command = [
            "ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", "ConnectTimeout=10", "-p", str(host.port),
        ]
        if host.user:
            command += ["-l", host.user]
        return command + [host.address]

    def run(self, host: Host, argv: list[str], file: Path | None = None) -> HostResult:
        cmd = self._ssh(host) + [shlex.join(argv)]
        try:
            if file is None:
                result = subprocess.run(cmd, capture_output=True, check=False, timeout=600)
            else:
                with file.open("rb") as stream:
                    result = subprocess.run(
                        cmd, stdin=stream, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, check=False, timeout=3600,
                    )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise OrchestrationError(f"{host.name}: SSH failed: {exc}") from exc
        return HostResult(
            host.name, argv[0], result.returncode,
            result.stdout.decode(errors="replace"),
            result.stderr.decode(errors="replace"),
        )

    def require(self, host: Host, argv: list[str], file: Path | None = None) -> str:
        result = self.run(host, argv, file)
        if result.code:
            raise OrchestrationError(
                f"{host.name}: remote command failed ({result.code}): "
                f"{result.error.strip() or result.output.strip()}"
            )
        return result.output.strip()

    def workspace(self, host: Host) -> str:
        remote = self.require(host, ["mktemp", "-d", "/tmp/platformctl-XXXXXXXX"])
        if not _WORKSPACE.fullmatch(remote):
            raise OrchestrationError(f"{host.name}: invalid remote staging directory: {remote!r}")
        return remote

    def put(self, host: Host, file: Path, destination: str) -> None:
        if not file.is_file():
            raise OrchestrationError(f"controller file not found: {file}")
        # The SSH user creates private files in a mode 0700 mktemp directory.
        self.require(host, ["sh", "-c", f"umask 077; cat > {shlex.quote(destination)}"], file)
        with file.open("rb") as stream:
            expected = hashlib.file_digest(stream, "sha256").hexdigest()
        actual = self.require(host, ["sha256sum", destination]).split()[0]
        if actual != expected:
            raise OrchestrationError(
                f"{host.name}: checksum mismatch for uploaded {file.name}"
            )

    def cleanup(self, host: Host, workspace: str) -> None:
        if not _WORKSPACE.fullmatch(workspace):
            raise OrchestrationError("unsafe remote staging path")
        self.require(host, ["rm", "-rf", "--", workspace])


def _effective_manifest(
    stack: StackManifest,
    software: tuple[ServiceSpec, ...],
    source_overrides: dict[str, str] | None = None,
    file_overrides: dict[str, str] | None = None,
) -> dict:
    source_overrides = source_overrides or {}
    file_overrides = file_overrides or {}
    specs: dict[str, dict] = {}
    for spec in software:
        item: dict = {"version": spec.version, "type": spec.type}
        if spec.root is not None:
            item["root"] = str(spec.root)
        else:
            assert spec.install_path is not None
            item["install_path"] = str(spec.install_path)
        if spec.source is not None:
            item["source"] = source_overrides.get(spec.name, spec.source)
        if spec.type == "service":
            item.update(
                service_name=spec.service_name,
                state=spec.state,
                autostart=spec.autostart,
            )
            if spec.service_file:
                item["service_file"] = file_overrides.get(
                    spec.name, str(spec.service_file)
                )
        else:
            assert spec.desktop_file is not None
            item["desktop_file"] = file_overrides.get(
                spec.name, str(spec.desktop_file)
            )
        specs[spec.name] = item
    # Global source is already resolved on the controller. Never reinterpret it
    # against a remote working directory or copy placement fields to a target.
    return {"schema": stack.schema, "platform": {"root": str(stack.platform_root)},
            "services": specs}


def _local_command(host: Host, command: str, manifest: Path, software: str | None) -> HostResult:
    argv = [command, "-f", str(manifest)]
    if software is not None:
        argv.insert(1, software)
    if command in _MUTATING and host.become and os.geteuid() != 0:
        executable = (
            [sys.executable] if getattr(sys, "frozen", False)
            else [sys.executable, "-m", "platformctl.cli"]
        )
        try:
            run = subprocess.run(
                ["sudo", "-n", *executable, *argv], capture_output=True, text=True,
                check=False, timeout=3600,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise OrchestrationError(f"{host.name}: local sudo execution failed: {exc}") from exc
        return HostResult(host.name, command, run.returncode, run.stdout, run.stderr)

    # Local means a direct Python CLI/Engine call, not SSH to our own host.
    from .cli import main

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    return HostResult(host.name, command, code, out.getvalue(), err.getvalue())


def _remote_command(
    host: Host, command: str, manifest: str, software: str | None
) -> list[str]:
    argv = ["platformctl", command]
    if software is not None:
        argv.append(software)
    argv += ["-f", manifest]
    if host.become and command in _MUTATING:
        argv = ["sudo", "-n", *argv]
    return argv


class Orchestrator:
    def __init__(self, transport: SSHTransport | None = None) -> None:
        self.transport = transport or SSHTransport()

    def _host(
        self, stack: StackManifest, target: Target, command: str,
        software: str | None = None,
    ) -> HostResult:
        host = target.host
        host.verify_local()
        if command not in _COMMANDS:
            raise OrchestrationError(f"unsupported remote command: {command}")
        if host.connection == "ssh":
            reported = self.transport.require(host, ["platformctl", "--version"])
            if reported != f"platformctl {__version__}":
                raise OrchestrationError(
                    f"{host.name}: executor version mismatch: {reported!r}; "
                    f"required platformctl {__version__}"
                )

        with tempfile.TemporaryDirectory(prefix="platformctl-controller-") as tmp:
            scratch = Path(tmp)
            if host.connection == "local":
                # Local execution can read the controller's existing sources and
                # configuration files directly, with no transfer or staging.
                filename = scratch / "stack.yaml"
                filename.write_text(
                    yaml.safe_dump(_effective_manifest(stack, target.software)),
                    encoding="utf-8",
                )
                return _local_command(host, command, filename, software)

            workspace = self.transport.workspace(host)
            try:
                source_overrides: dict[str, str] = {}
                file_overrides: dict[str, str] = {}
                for spec in target.software:
                    if spec.service_file or spec.desktop_file:
                        source = spec.service_file or spec.desktop_file
                        assert source is not None
                        # DesktopFileManager installs based on source filename:
                        # preserve the XDG desktop ID rather than staging under
                        # a generated name.
                        if spec.type == "desktop":
                            destination = f"{workspace}/{source.name}"
                        else:
                            destination = f"{workspace}/config-{spec.name}{source.suffix}"
                        self.transport.put(host, source, destination)
                        file_overrides[spec.name] = destination
                    if spec.source is None:
                        continue
                    location = artifact_for(spec)
                    source_at = spec.source_location or (
                        "target" if location.remote else "controller"
                    )
                    if source_at == "target":
                        # Already a resolvable absolute URL or target-local path.
                        continue
                    destination = f"{workspace}/artifact-{spec.name}.deb"
                    source_overrides[spec.name] = destination
                    if command != "install":
                        # Only the source flag is needed for dpkg registration
                        # checks. Do not transfer artifacts for status/apply.
                        continue
                    if location.remote:
                        downloaded = scratch / f"artifact-{spec.name}.deb"
                        PackageManager._download(location.location, downloaded)
                        local_artifact = downloaded
                    else:
                        local_artifact = Path(location.location)
                    self.transport.put(host, local_artifact, destination)
                filename = scratch / "stack.yaml"
                filename.write_text(
                    yaml.safe_dump(_effective_manifest(
                        stack, target.software, source_overrides, file_overrides,
                    )),
                    encoding="utf-8",
                )
                remote_manifest = workspace + "/stack.yaml"
                self.transport.put(host, filename, remote_manifest)
                result = self.transport.run(
                    host, _remote_command(host, command, remote_manifest, software)
                )
                return HostResult(host.name, command, result.code, result.output, result.error)
            finally:
                self.transport.cleanup(host, workspace)

    def _privilege_check(self, target: Target) -> HostResult:
        """Preflight root execution on every host before distributed activation."""
        host = target.host
        if not host.become:
            return HostResult(host.name, "sudo-preflight", 0, "no sudo requested")
        if host.connection == "local" and os.geteuid() == 0:
            return HostResult(host.name, "sudo-preflight", 0, "already root")
        if host.connection == "ssh":
            check = self.transport.run(host, ["sudo", "-n", "platformctl", "--version"])
        else:
            executable = (
                [sys.executable] if getattr(sys, "frozen", False)
                else [sys.executable, "-m", "platformctl.cli"]
            )
            try:
                process = subprocess.run(
                    ["sudo", "-n", *executable, "--version"],
                    capture_output=True, text=True, check=False, timeout=30,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise OrchestrationError(f"{host.name}: sudo preflight failed: {exc}") from exc
            check = HostResult(
                host.name, "sudo-preflight", process.returncode,
                process.stdout, process.stderr,
            )
        if check.code != 0 or check.output.strip() != f"platformctl {__version__}":
            raise OrchestrationError(
                f"{host.name}: sudo -n cannot run matching platformctl: "
                f"{check.error.strip() or check.output.strip()}"
            )
        return HostResult(host.name, "sudo-preflight", 0, check.output)

    def execute(
        self, stack: StackManifest, targets: tuple[Target, ...], command: str,
        *, software: str | None = None, parallel: int = 1,
    ) -> list[HostResult]:
        if parallel < 1 or parallel > 32:
            raise OrchestrationError("--parallel must be between 1 and 32")
        if command == "apply":
            # No changes until all hosts pass release readiness AND sudo checks.
            checks = self.execute(stack, targets, "check", parallel=parallel)
            if not any(result.code for result in checks):
                def privilege(target: Target) -> HostResult:
                    try:
                        return self._privilege_check(target)
                    except Exception as exc:
                        return HostResult(target.host.name, "sudo-preflight", 2, "", str(exc))

                # Avoid concurrent local preflight side-effects with other threads.
                checks = [privilege(t) for t in targets]
            if any(result.code for result in checks):
                return [
                    HostResult(
                        r.host, "apply", 2, "",
                        "preflight failed: " + (r.error or r.output).strip()
                        if r.code else "activation aborted: another host failed preflight",
                    ) for r in checks
                ]

        def run(target: Target) -> HostResult:
            try:
                return self._host(stack, target, command, software)
            except Exception as exc:
                return HostResult(target.host.name, command, 2, "", str(exc))

        if parallel == 1:
            return [run(t) for t in targets]
        # redirect_stdout/redirect_stderr in the in-process LocalExecutor are
        # process-global, not thread-local. Execute the controller host first,
        # then fan out SSH workers, preserving original inventory order.
        completed: dict[str, HostResult] = {}
        for target in targets:
            if target.host.connection == "local":
                completed[target.host.name] = run(target)
        remote = [target for target in targets if target.host.connection == "ssh"]
        with ThreadPoolExecutor(max_workers=parallel) as pool:
            results = list(pool.map(run, remote))
        completed.update((result.host, result) for result in results)
        return [completed[target.host.name] for target in targets]
