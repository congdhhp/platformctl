from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlsplit

from .errors import ManifestError
from .models import ServiceSpec


@dataclass(frozen=True)
class ArtifactLocation:
    location: str
    remote: bool


def _source_kind(value: str) -> str:
    """Classify URL/absolute local/relative local without accessing the network."""
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ManifestError("source must be a non-empty path or HTTP(S) URL")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ManifestError("source must not contain control characters")
    if value.startswith("//"):
        raise ManifestError("source must not begin with '//'")

    parsed = urlsplit(value)
    if parsed.scheme:
        if parsed.scheme not in {"http", "https"}:
            raise ManifestError("source supports only local paths and HTTP(S) URLs")
        if not parsed.netloc or parsed.username or parsed.password:
            raise ManifestError("source URL must have a host and no embedded credentials")
        if parsed.query or parsed.fragment or "?" in value or "#" in value:
            raise ManifestError("source URL must not have query strings or fragments")
        if not parsed.path or not (parsed.path.endswith("/") or parsed.path.endswith(".deb")):
            raise ManifestError("source URL must end in '/' or '.deb'")
        return "remote"

    if "\\" in value:
        raise ManifestError("source paths must use '/' separators")
    return "local-absolute" if Path(value).is_absolute() else "relative"


def _normalized_local(source: str, project_directory: Path) -> str:
    is_dir = source.endswith("/")
    path = Path(source)
    path = path.resolve() if path.is_absolute() else (project_directory / path).resolve()
    return str(path) + ("/" if is_dir and str(path) != "/" else "")


def normalize_global_source(source: str, project_directory: Path) -> str:
    """The optional platform source is a directory, never an individual .deb."""
    kind = _source_kind(source)
    if not source.endswith("/"):
        raise ManifestError("platform.source must be a directory ending in '/'")
    if kind == "remote":
        return source
    return _normalized_local(source, project_directory)


def normalize_service_source(
    source: str, project_directory: Path, global_source: str | None = None
) -> str:
    """Resolve once at manifest load time; downstream consumers see absolute sources.

    With a platform source, relative paths are appended to it. Otherwise they
    are relative to the project directory. Neither rule depends on process cwd.
    """
    kind = _source_kind(source)
    if kind == "remote":
        return source
    if kind == "local-absolute":
        return _normalized_local(source, project_directory)

    if global_source is None:
        return _normalized_local(source, project_directory)

    parts = PurePosixPath(source).parts
    if ".." in parts:
        raise ManifestError("relative service source must not contain '..' with platform.source")
    # '.' does not override a global source. Both './controller/' and 'controller/'
    # use the same base. URL path segments must be encoded independently.
    relative_parts = [part for part in parts if part not in {"", "."}]
    relative = "/".join(relative_parts)
    if not relative:
        raise ManifestError("service source must identify a directory or .deb file")
    suffix = "/" if source.endswith("/") else ""
    if _source_kind(global_source) == "remote":
        result = global_source + "/".join(quote(part, safe="") for part in relative_parts) + suffix
    else:
        result = str((Path(global_source) / relative).resolve()) + suffix
    _source_kind(result)
    return result


def resolve_source(name: str, version: str, source: str) -> ArtifactLocation:
    """Resolve direct .deb files and versioned directory sources.

    Directory convention: <source>/<version>/<name>-<version>.deb
    The supplied source has already been normalized by the manifest loader.
    """
    kind = _source_kind(source)
    if kind == "relative":
        raise ManifestError("artifact source must be resolved before use")
    remote = kind == "remote"
    if source.endswith(".deb"):
        return ArtifactLocation(source, remote)
    if not source.endswith("/"):
        raise ManifestError("directory source must end in '/'")
    if remote:
        return ArtifactLocation(
            source + quote(version, safe="") + "/" + quote(f"{name}-{version}.deb", safe=""),
            True,
        )
    return ArtifactLocation(str(Path(source) / version / f"{name}-{version}.deb"), False)


def artifact_for(spec: ServiceSpec) -> ArtifactLocation:
    if spec.source is None:
        raise ManifestError(f"{spec.name}: no source configured")
    return resolve_source(spec.name, spec.version, spec.source)
