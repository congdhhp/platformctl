from __future__ import annotations

import posixpath
import re
import subprocess
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath

from .artifacts import artifact_for
from .errors import PackageError
from .models import ServiceSpec
from .release import ensure_release_exists, validate_managed_links

_DEBIAN_ID = re.compile(r"^[a-z0-9][a-z0-9+.-]*$")
_SCRIPTS = {"preinst", "postinst", "prerm", "postrm", "config", "triggers"}
_MAX_DOWNLOAD_BYTES = 8 * 1024 * 1024 * 1024


def expected_package_name(spec: ServiceSpec) -> str:
    """Use a distinct Debian package identity for each immutable release."""
    name = f"{spec.name.lower()}-release-{spec.version.lower()}"
    if not _DEBIAN_ID.fullmatch(name) or spec.version != spec.version.lower():
        raise PackageError(
            f"{spec.name}: cannot derive co-installable Debian package name {name!r}; "
            "use lowercase release identifiers with letters, digits, '.', '+' or '-'"
        )
    return name


class PackageManager:
    """Acquire, inspect and install co-installable, versioned .deb packages."""

    @staticmethod
    def _run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                args, text=True, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, check=False,
            )
        except OSError as exc:
            raise PackageError(f"cannot execute {args[0]}: {exc}") from exc
        if check and result.returncode != 0:
            raise PackageError(
                f"{' '.join(args[:2])} failed: "
                f"{(result.stderr or result.stdout).strip() or result.returncode}"
            )
        return result

    def installed_package_version(self, package: str) -> str | None:
        result = self._run(
            "dpkg-query", "-W", "-f=${Status}\t${Version}", package,
            check=False,
        )
        if result.returncode != 0:
            return None
        status, separator, version = result.stdout.strip().partition("\t")
        return version if separator and status == "install ok installed" and version else None

    def check(self, spec: ServiceSpec) -> tuple[bool, str]:
        """Read-only readiness check; legacy pre-deployed releases remain valid."""
        if not spec.managed:
            if spec.install_path is not None and spec.install_path.is_dir():
                return True, "external installation (version unverified)"
            return False, f"installation directory missing: {spec.install_path}"
        try:
            validate_managed_links(spec)
            ensure_release_exists(spec)
        except Exception as exc:
            return False, str(exc)
        if spec.source is None:
            return True, "release directory present (not tracked by dpkg)"
        package = expected_package_name(spec)
        version = self.installed_package_version(package)
        if version != spec.version:
            return False, (
                f"dpkg package {package} has version {version or 'not installed'}; "
                f"expected {spec.version}"
            )
        return True, f"dpkg package {package} ({version}) installed"

    @staticmethod
    def _download(url: str, destination: Path) -> None:
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "platformctl/0.1"})
            with urllib.request.urlopen(request, timeout=60) as response:
                total = 0
                with destination.open("wb") as stream:
                    while chunk := response.read(1024 * 1024):
                        total += len(chunk)
                        if total > _MAX_DOWNLOAD_BYTES:
                            raise PackageError("artifact exceeds maximum download size")
                        stream.write(chunk)
        except (urllib.error.URLError, OSError) as exc:
            raise PackageError(f"could not fetch {url}: {exc}") from exc

    def _inspect_payload(self, spec: ServiceSpec, deb: Path) -> None:
        expected = PurePosixPath(str((spec.version_root / spec.version).resolve()))
        try:
            proc = subprocess.Popen(
                ["dpkg-deb", "--fsys-tarfile", str(deb)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise PackageError(f"cannot inspect .deb payload: {exc}") from exc
        try:
            assert proc.stdout is not None
            assert proc.stderr is not None
            with tarfile.open(fileobj=proc.stdout, mode="r|") as archive:
                for member in archive:
                    if member.name.startswith("/"):
                        raise PackageError(f"absolute payload path: {member.name}")
                    parts = PurePosixPath(member.name).parts
                    if parts and parts[0] == ".":
                        parts = parts[1:]
                    if ".." in parts:
                        raise PackageError(f"path traversal in package: {member.name}")
                    path = PurePosixPath("/").joinpath(*parts)
                    if path == PurePosixPath("/") and member.isdir():
                        continue
                    within = path == expected or expected in path.parents
                    ancestor = member.isdir() and path in expected.parents
                    if not (within or ancestor):
                        raise PackageError(
                            f"{spec.name}: .deb writes outside version directory: {path}"
                        )
                    if member.isdev() or member.isfifo():
                        raise PackageError(f"special file in package: {path}")
                    if member.issym() or member.islnk():
                        if member.issym():
                            link = posixpath.normpath(
                                posixpath.join(str(path.parent), member.linkname)
                            )
                        else:
                            link = posixpath.normpath(
                                member.linkname if member.linkname.startswith("/")
                                else "/" + member.linkname
                            )
                        linkpath = PurePosixPath(link)
                        if linkpath != expected and expected not in linkpath.parents:
                            raise PackageError(
                                f"{spec.name}: package link escapes version directory: {path}"
                            )
            error = proc.stderr.read().decode(errors="replace")
            if proc.wait() != 0:
                raise PackageError(f"dpkg-deb failed to inspect payload: {error.strip()}")
        except (OSError, tarfile.TarError) as exc:
            raise PackageError(f"cannot inspect .deb payload: {exc}") from exc
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            if proc.stdout is not None:
                proc.stdout.close()
            if proc.stderr is not None:
                proc.stderr.close()

    def _inspect(
        self, spec: ServiceSpec, deb: Path, scratch: Path,
        target_arch: str | None = None,
    ) -> str:
        expected = expected_package_name(spec)
        actual = self._run("dpkg-deb", "-f", str(deb), "Package").stdout.strip()
        version = self._run("dpkg-deb", "-f", str(deb), "Version").stdout.strip()
        arch = self._run("dpkg-deb", "-f", str(deb), "Architecture").stdout.strip()
        host = target_arch or self._run("dpkg", "--print-architecture").stdout.strip()
        if actual != expected:
            raise PackageError(
                f"{spec.name}: .deb package identity {actual!r} != {expected!r}; "
                "each release needs its own Debian package name"
            )
        if version != spec.version:
            raise PackageError(
                f"{spec.name}: Debian Version {version!r} != desired {spec.version!r}"
            )
        if arch not in {host, "all"}:
            raise PackageError(
                f"{spec.name}: package architecture {arch!r} != host {host!r}"
            )
        control = scratch / "control"
        control.mkdir()
        self._run("dpkg-deb", "-e", str(deb), str(control))
        scripts = sorted(_SCRIPTS.intersection(p.name for p in control.iterdir()))
        if scripts:
            raise PackageError(
                f"{spec.name}: maintainer scripts/triggers {', '.join(scripts)} present. "
                "Install refused: version-specific packages must not change shared "
                "services or run lifecycle scripts; rebuild without these files."
            )
        self._inspect_payload(spec, deb)
        return version

    def install(self, spec: ServiceSpec) -> str:
        if not spec.managed:
            return "external installation (not managed)"
        validate_managed_links(spec)
        package = expected_package_name(spec) if spec.source is not None else None
        installed = self.installed_package_version(package) if package else None
        target = spec.version_root / spec.version
        if target.exists() or target.is_symlink():
            ensure_release_exists(spec)
            if spec.source is None or installed == spec.version:
                return "already installed"
            if installed is not None:
                raise PackageError(
                    f"{spec.name}: installed Debian Version {installed!r} "
                    f"does not match desired {spec.version!r}"
                )
            raise PackageError(
                f"{spec.name}: release directory exists but package {package} is not "
                "registered by dpkg; refusing to overwrite the release"
            )
        if installed is not None:
            raise PackageError(
                f"{spec.name}: dpkg reports {package} installed but the release "
                "directory is missing; refusing to reinstall"
            )
        if spec.source is None:
            raise PackageError(
                f"{spec.name}: desired release not installed and source is not configured"
            )
        location = artifact_for(spec)
        with tempfile.TemporaryDirectory(prefix="platformctl-install-") as work:
            scratch = Path(work)
            if location.remote:
                deb = scratch / "release.deb"
                self._download(location.location, deb)
            else:
                deb = Path(location.location)
                if not deb.is_file():
                    raise PackageError(f"artifact not found: {deb}")
            actual_version = self._inspect(spec, deb, scratch)
            self._run("dpkg", "-i", str(deb))
        if self.installed_package_version(package) != actual_version:
            raise PackageError(f"{spec.name}: dpkg package installation cannot be verified")
        ensure_release_exists(spec)
        return f"installed {package} ({actual_version})"
