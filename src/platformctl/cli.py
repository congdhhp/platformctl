from __future__ import annotations

import argparse
import sys

from . import __version__
from .engine import Engine
from .errors import PlatformCtlError
from .manifest import load_manifest
from .release import current_version, list_deployed_releases, previous_version


def _manifest(args: argparse.Namespace):
    return load_manifest(args.file)


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
        return p

    with_file("validate", "validate manifest and referenced releases")
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
            print("OK")
            return 0

        if args.command == "status":
            _print_status(engine.status(manifest))
            return 0

        if args.command == "diff":
            changed = False
            for spec in manifest.services.values():
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
                print("No managed version changes")
            return 0

        if args.command == "apply":
            for name, old, new in engine.apply(manifest):
                spec = manifest.services[name]
                if not spec.managed:
                    print(
                        f"{name}: external at {spec.install_path} "
                        "(systemd state reconciled; version unverified)"
                    )
                elif old == new:
                    print(f"{name}: {new} (unchanged)")
                else:
                    print(f"{name}: {old or '-'} -> {new}")
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
