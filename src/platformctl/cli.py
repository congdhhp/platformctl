from __future__ import annotations

import argparse
import sys

from . import __version__
from .inventory import load_inventory
from .planner import build_targets
from .orchestration import Orchestrator
from .artifacts import artifact_for
from .engine import Engine
from .errors import PlatformCtlError
from .manifest import load_manifest
from .packages import PackageManager
from .release import current_version, list_deployed_releases, previous_version


def _manifest(args: argparse.Namespace):
    return load_manifest(args.file, project_directory=args.project_directory)


def _print_status(rows) -> None:
    print(f"{'SOFTWARE':20} {'TYPE':9} {'DESIRED':24} {'CURRENT':24} {'MODE':9} {'STATE':12} {'AUTOSTART':9}")
    for row in rows:
        print(
            f"{row.name:20} {row.type:9} {row.desired:24} "
            f"{(row.current or ('unknown' if row.mode == 'external' else '-')):24} "
            f"{row.mode:9} {row.state:12} "
            f"{('-' if row.enabled is None else 'yes' if row.enabled else 'no'):9}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="platformctl",
        description="Manage native Linux service and desktop application releases",
    )
    parser.add_argument("--version", action="version", version=f"platformctl {__version__}")
    parser.add_argument("-i", "--inventory", default=None, help="optional multi-host inventory")
    parser.add_argument("--limit", default=None, help="select hosts or groups, comma separated")
    parser.add_argument("--parallel", type=int, default=1, help="concurrent hosts (1..32)")
    parser.add_argument(
        "--project-directory", default=None,
        help="base for relative local paths (default: directory containing stack.yaml)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def with_file(name: str, help_text: str):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("-f", "--file", default="stack.yaml", help="stack manifest path")
        # Accept both: platformctl --project-directory DIR install ...
        # and:       platformctl install --project-directory DIR ...
        # SUPPRESS keeps a parent-level option from being overwritten by a
        # subparser default when the option is placed before the command.
        p.add_argument(
            "--project-directory", default=argparse.SUPPRESS,
            help="base for relative local paths (default: directory containing stack.yaml)",
        )
        # Allow the same controller options before or after each subcommand.
        p.add_argument("-i", "--inventory", default=argparse.SUPPRESS,
                       help="optional multi-host inventory")
        p.add_argument("--limit", default=argparse.SUPPRESS,
                       help="select hosts or groups, comma separated")
        p.add_argument("--parallel", type=int, default=argparse.SUPPRESS,
                       help="concurrent hosts (1..32)")
        return p

    validate = with_file("validate", "validate manifest configuration without requiring releases")
    validate.add_argument(
        "--show-sources", action="store_true",
        help="show resolved artifact locations without downloading them",
    )
    with_file("plan", "show host placements and artifact transfer plan (no SSH)")
    with_file("install", "download and install missing .deb releases without activation")
    with_file("check", "read-only readiness check for desired installed releases")
    with_file("status", "show desired/current versions and runtime/launcher state")
    with_file("diff", "show services whose desired version differs from current")
    with_file("apply", "activate desired releases and reconcile systemd/desktop integration")

    rollback = with_file("rollback", "rollback one service to its previous release")
    rollback.add_argument("service", help="service name from the manifest")

    list_rel = with_file("list-releases", "list deployed releases for one service")
    list_rel.add_argument("service", help="service name from the manifest")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = _manifest(args)
        if args.parallel < 1 or args.parallel > 32:
            raise PlatformCtlError("--parallel must be between 1 and 32")

        if args.inventory is not None:
            inventory = load_inventory(args.inventory)
            targets = build_targets(manifest, inventory, args.limit)
            if args.command == "validate":
                # Configuration validation does not require network connectivity.
                Engine().validate(manifest)
                if args.show_sources:
                    for target in targets:
                        for spec in target.software:
                            source = artifact_for(spec).location if spec.source else "(pre-installed)"
                            print(f"{target.host.name} {spec.name}: {source}")
                print(f"OK: {len(targets)} host(s), "
                      f"{sum(len(t.software) for t in targets)} placement(s)")
                return 0
            if args.command == "plan":
                print(f"{'HOST':20} {'CONNECTION':11} {'SOFTWARE':20} {'ARTIFACT DELIVERY'}")
                for target in targets:
                    for spec in target.software:
                        if spec.source is None:
                            delivery = "pre-installed"
                        else:
                            artifact = artifact_for(spec)
                            at = spec.source_location or (
                                "target" if artifact.remote else "controller"
                            )
                            if target.host.connection == "local":
                                delivery = "local acquisition"
                            elif at == "controller":
                                delivery = "download + SSH push" if artifact.remote else "SSH push"
                            else:
                                delivery = "target download" if artifact.remote else "target-local"
                        print(f"{target.host.name:20} {target.host.connection:11} "
                              f"{spec.name:20} {delivery}")
                return 0
            filtered = []
            for target in targets:
                selected = tuple(
                    s for s in target.software
                    if args.command not in {"rollback", "list-releases"} or s.name == args.service
                )
                if selected:
                    from .planner import Target
                    filtered.append(Target(target.host, selected))
            if not filtered:
                raise PlatformCtlError("software not found on selected host(s)")
            results = Orchestrator().execute(
                manifest, tuple(filtered), args.command,
                software=args.service if args.command in {"rollback", "list-releases"} else None,
                parallel=args.parallel,
            )
            for result in results:
                print(f"[{result.host}] {result.command}: "
                      f"{'OK' if result.code == 0 else 'FAILED'}")
                if result.output.strip():
                    print(result.output.rstrip())
                if result.error.strip():
                    print(result.error.rstrip(), file=sys.stderr)
            return 2 if any(r.code for r in results) else 0

        if args.limit is not None:
            raise PlatformCtlError("--limit requires --inventory")
        if args.command == "plan":
            raise PlatformCtlError("plan requires --inventory")
        if any(spec.hosts is not None for spec in manifest.services.values()):
            raise PlatformCtlError("services.*.hosts requires --inventory")
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
                if spec.type == "desktop":
                    if engine.desktop_files.prepare(spec) is not None:
                        changed = True
                        print(
                            f"{spec.name}: desktop launcher "
                            f"{spec.desktop_file.name} will be updated"
                        )
                elif engine.unit_files.prepare(spec) is not None:
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
                if spec.type == "service" and engine.unit_files.prepare(spec) is not None
            }
            pending_desktops = {
                name for name, spec in manifest.services.items()
                if spec.type == "desktop" and engine.desktop_files.prepare(spec) is not None
            }
            for name, old, new in engine.apply(manifest):
                spec = manifest.services[name]
                suffix = (
                    ", systemd unit updated" if name in pending_units
                    else ", desktop launcher updated" if name in pending_desktops
                    else ""
                )
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
