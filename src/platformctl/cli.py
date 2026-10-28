from __future__ import annotations

import argparse
import sys

from . import __version__
from .artifacts import artifact_for
from .engine import Engine
from .errors import PlatformCtlError
from .manifest import load_manifest
from .packages import PackageManager
from .release import current_version, list_deployed_releases, previous_version


def _manifest(args: argparse.Namespace):
    return load_manifest(args.file, project_directory=args.project_directory)


def _print_status(rows) -> None:
    print(f"{'SERVICE':20} {'DESIRED':24} {'CURRENT':24} {'MODE':9} {'STATE':10} {'AUTOSTART':9}")
    for row in rows:
        print(
            f"{row.name:20} {row.desired:24} "
            f"{(row.current or ('unknown' if row.mode == 'external' else '-')):24} "
            f"{row.mode:9} {row.state:10} {('yes' if row.enabled else 'no'):9}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="platformctl",
        description="Manage native Linux service releases",
    )
    parser.add_argument("--version", action="version", version=f"platformctl {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def with_file(name: str, help_text: str):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("-f", "--file", default="stack.yaml", help="stack manifest path")
        p.add_argument(
            "--project-directory", default=None,
            help="base for relative local paths (default: directory containing stack.yaml)",
        )
        return p

    validate = with_file("validate", "validate manifest configuration without requiring releases")
    validate.add_argument(
        "--show-sources", action="store_true",
        help="show resolved artifact locations without downloading them",
    )
    with_file("install", "download and install missing .deb releases without activation")
    with_file("check", "read-only readiness check for desired installed releases")
    with_file("status", "show desired/current versions and systemd state")
    with_file("diff", "show services whose desired version differs from current")
    with_file("apply", "activate desired versions and reconcile systemd state")

    rollback = with_file("rollback", "rollback one service to its previous release")
    rollback.add_argument("service", help="service name from the manifest")

    list_rel = with_file("list-releases", "list deployed releases for one service")
    list_rel.add_argument("service", help="service name from the manifest")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = _manifest(args)
        engine = Engine()

        if args.command == "validate":
            engine.validate(manifest)
            if args.show_sources:
                print(f"Project directory: {manifest.project_directory}")
                print(f"Global source: {manifest.global_source or '-'}")
                print("Resolved artifact sources:")
                for spec in manifest.services.values():
                    location = (
                        artifact_for(spec).location if spec.source is not None
                        else "(pre-installed; no source)"
                    )
                    print(f"  {spec.name}: {location}")
            print("OK")
            return 0

        if args.command == "install":
            engine.validate(manifest)
            packages = PackageManager()
            for spec in manifest.services.values():
                result = packages.install(spec)
                print(f"{spec.name}: {result}")
            return 0

        if args.command == "check":
            engine.validate(manifest)
            packages = PackageManager()
            ready = True
            for spec in manifest.services.values():
                ok, detail = packages.check(spec)
                print(f"{spec.name}: {'READY' if ok else 'MISSING'} — {detail}")
                ready = ready and ok
            print("All desired releases ready" if ready else "Some releases are not ready")
            return 0 if ready else 2

        if args.command == "status":
            _print_status(engine.status(manifest))
            return 0

        if args.command == "diff":
            changed = False
            for spec in manifest.services.values():
                if engine.unit_files.prepare(spec) is not None:
                    changed = True
                    print(f"{spec.name}: systemd unit {spec.service_name} will be updated")
                if not spec.managed:
                    print(
                        f"{spec.name}: external installation at {spec.install_path}; "
                        "version cannot be verified"
                    )
                    continue
                current = current_version(spec)
                if current != spec.version:
                    changed = True
                    print(f"{spec.name}: {current or '-'} -> {spec.version}")
            if not changed:
                print("No managed version or systemd unit changes")
            return 0

        if args.command == "apply":
            engine.validate(manifest)
            pending_units = {
                name for name, spec in manifest.services.items()
                if engine.unit_files.prepare(spec) is not None
            }
            for name, old, new in engine.apply(manifest):
                spec = manifest.services[name]
                suffix = ", systemd unit updated" if name in pending_units else ""
                if not spec.managed:
                    print(
                        f"{name}: external at {spec.install_path} "
                        f"(systemd state reconciled{suffix}; version unverified)"
                    )
                elif old == new:
                    print(f"{name}: {new} (unchanged version{suffix})")
                else:
                    print(f"{name}: {old or '-'} -> {new}{suffix}")
            return 0

        if args.command in {"rollback", "list-releases"}:
            spec = manifest.services.get(args.service)
            if spec is None:
                raise PlatformCtlError(f"service not found in manifest: {args.service}")

            if args.command == "rollback":
                old, new = engine.rollback(spec)
                print(f"{spec.name}: {old or '-'} -> {new}")
                return 0

            if not spec.managed:
                raise PlatformCtlError(
                    f"{spec.name} uses install_path; list-releases is unavailable"
                )
            current = current_version(spec)
            previous = previous_version(spec)
            for version in list_deployed_releases(spec):
                flags = []
                if version == current:
                    flags.append("current")
                if version == previous:
                    flags.append("previous")
                suffix = f" ({', '.join(flags)})" if flags else ""
                print(f"{version}{suffix}")
            return 0

        raise AssertionError(args.command)
    except PlatformCtlError as exc:
        print(f"platformctl: error: {exc}", file=sys.stderr)
        return 2
    except PermissionError as exc:
        print(f"platformctl: permission denied: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
