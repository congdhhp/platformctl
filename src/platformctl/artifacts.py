from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit

from .errors import ManifestError
from .models import ServiceSpec


@dataclass(frozen=True)
class ArtifactLocation:
    location: str
    remote: bool


def resolve_source(name: str, version: str, source: str) -> ArtifactLocation:
    """Resolve a direct .deb or a convention-based per-software directory.

    Directory convention: <source>/<version>/<name>-<version>.deb
    Local sources are absolute paths (resolved by the manifest parser).
    """
    if not isinstance(source, str) or not source.strip():
        raise ManifestError("source must be a non-empty local path or HTTP(S) URL")

    parsed = urlsplit(source)
    remote = parsed.scheme in {"http", "https"}
    if remote:
        if not parsed.netloc or parsed.username or parsed.password:
            raise ManifestError(f"invalid or credential-bearing source URL: {source}")
        if parsed.query or parsed.fragment:
            raise ManifestError("source URLs must not contain query strings or fragments")
        if parsed.path.endswith(".deb"):
            return ArtifactLocation(source, True)
        if not parsed.path.endswith("/"):
            raise ManifestError("directory source URL must end in '/'")
        return ArtifactLocation(
            source
            + quote(version, safe="")
            + "/"
            + quote(f"{name}-{version}.deb", safe=""),
            True,
        )

    if parsed.scheme or source.startswith("//"):
        raise ManifestError("source must be a local path or an http(s) URL")
    if source.endswith(".deb"):
        return ArtifactLocation(source, False)
    if not source.endswith("/"):
        raise ManifestError("directory source must end in '/'")
    return ArtifactLocation(str(Path(source) / version / f"{name}-{version}.deb"), False)


def artifact_for(spec: ServiceSpec) -> ArtifactLocation:
    if spec.source is None:
        raise ManifestError(f"{spec.name}: no source configured")
    return resolve_source(spec.name, spec.version, spec.source)
