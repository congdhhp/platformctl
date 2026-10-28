class PlatformCtlError(Exception):
    """Base exception for user-facing platformctl failures."""


class ManifestError(PlatformCtlError):
    """The stack manifest is invalid."""


class ReleaseError(PlatformCtlError):
    """A release layout or activation operation failed."""


class SystemdError(PlatformCtlError):
    """A systemd command failed."""


class ApplyError(PlatformCtlError):
    """Applying desired state failed."""


class PackageError(PlatformCtlError):
    """Acquiring, checking or installing a Debian release failed."""
