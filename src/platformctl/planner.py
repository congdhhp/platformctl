"""Map software placements to hosts; no networking or side effects."""
from __future__ import annotations

from dataclasses import dataclass

from .errors import ManifestError
from .inventory import Host, Inventory
from .models import ServiceSpec, StackManifest


@dataclass(frozen=True)
class Target:
    host: Host
    software: tuple[ServiceSpec, ...]


def build_targets(
    stack: StackManifest, inventory: Inventory, limit: str | None = None
) -> tuple[Target, ...]:
    local_hosts = [h for h in inventory.hosts.values() if h.connection == "local"]
    if len(local_hosts) > 1:
        raise ManifestError("inventory must contain no more than one local connection")
    implicit_local = local_hosts[0] if local_hosts else Host("localhost", "local", become=False)
    targets: dict[str, tuple[Host, list[ServiceSpec]]] = {}
    for spec in stack.services.values():
        names = inventory.expand(spec.hosts) if spec.hosts else (implicit_local.name,)
        for name in names:
            host = inventory.hosts.get(name, implicit_local)
            host.verify_local()
            if name not in targets:
                targets[name] = (host, [])
            targets[name][1].append(spec)

    if limit:
        selectors = tuple(x.strip() for x in limit.split(","))
        if any(not x for x in selectors):
            raise ManifestError("--limit needs a comma-separated host or group name")
        chosen: set[str] = set()
        for selector in selectors:
            if selector == "localhost" and implicit_local.name == "localhost":
                chosen.add("localhost")
            else:
                chosen.update(inventory.expand((selector,)))
        targets = {key: value for key, value in targets.items() if key in chosen}
    if not targets:
        raise ManifestError("no software matches selected hosts")
    return tuple(Target(host, tuple(specs)) for host, specs in targets.values())
