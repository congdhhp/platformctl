"""Small, explicit, agentless inventory. No Ansible runtime dependency."""
from __future__ import annotations

import re
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import yaml

from .errors import ManifestError

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_USER = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
_ADDRESS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:-]*$")
_FIELDS = {"connection", "address", "user", "port", "become", "hostname"}


@dataclass(frozen=True)
class Host:
    name: str
    connection: str = "ssh"
    address: str | None = None
    user: str | None = None
    port: int = 22
    become: bool = True
    hostname: str | None = None

    def verify_local(self) -> None:
        """Optional positive host binding prevents accidental deployments on a copied controller."""
        if self.connection == "local" and self.hostname:
            actual = socket.gethostname()
            if actual != self.hostname:
                raise ManifestError(
                    f"{self.name}: connection=local requires hostname "
                    f"{self.hostname!r}, but current machine is {actual!r}"
                )


@dataclass(frozen=True)
class Inventory:
    hosts: dict[str, Host]
    groups: dict[str, tuple[str, ...]]

    def expand(self, selectors: tuple[str, ...]) -> tuple[str, ...]:
        result: list[str] = []
        for selector in selectors:
            if selector in self.hosts:
                members = (selector,)
            elif selector in self.groups:
                members = self.groups[selector]
            else:
                raise ManifestError(f"unknown host/group selector: {selector}")
            for name in members:
                if name not in result:
                    result.append(name)
        return tuple(result)


def _mapping(value: object, label: str) -> Mapping:
    if not isinstance(value, Mapping):
        raise ManifestError(f"{label} must be a mapping")
    return value


def _unknown(data: Mapping, allowed: set[str], label: str) -> None:
    extra = set(data) - allowed
    if extra:
        raise ManifestError(f"{label} has unknown fields: {', '.join(sorted(map(str, extra)))}")


def load_inventory(path: str | Path) -> Inventory:
    filename = Path(path).resolve()
    try:
        data = yaml.safe_load(filename.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ManifestError(f"cannot read inventory {filename}: {exc}") from exc
    top = _mapping(data, "inventory")
    _unknown(top, {"defaults", "hosts", "groups"}, "inventory")
    defaults = _mapping(top.get("defaults", {}), "inventory.defaults")
    _unknown(defaults, _FIELDS, "inventory.defaults")
    host_data = _mapping(top.get("hosts"), "inventory.hosts")
    if not host_data:
        raise ManifestError("inventory.hosts cannot be empty")
    hosts: dict[str, Host] = {}
    for name, raw in host_data.items():
        if not isinstance(name, str) or not _ID.fullmatch(name):
            raise ManifestError(f"invalid inventory hostname: {name!r}")
        cfg = _mapping(raw, f"inventory.hosts.{name}")
        _unknown(cfg, _FIELDS, f"inventory.hosts.{name}")
        values = {**defaults, **cfg}
        connection = values.get("connection", "ssh")
        if connection not in {"ssh", "local"}:
            raise ManifestError(f"{name}: connection must be 'ssh' or 'local'")
        address = values.get("address")
        user = values.get("user")
        port = values.get("port", 22)
        become = values.get("become", True)
        hostname = values.get("hostname")
        if connection == "ssh":
            if not isinstance(address, str) or not _ADDRESS.fullmatch(address):
                raise ManifestError(f"{name}: SSH address must be a DNS name or IP address")
            if user is not None and (not isinstance(user, str) or not _USER.fullmatch(user)):
                raise ManifestError(f"{name}: invalid SSH user")
        elif address is not None:
            raise ManifestError(f"{name}: local connection must not specify address")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ManifestError(f"{name}: port must be 1..65535")
        if type(become) is not bool:
            raise ManifestError(f"{name}: become must be boolean")
        if hostname is not None and (not isinstance(hostname, str) or not _ID.fullmatch(hostname)):
            raise ManifestError(f"{name}: invalid hostname binding")
        host = Host(name, connection, address, user, port, become, hostname)
        host.verify_local()
        hosts[name] = host

    groups_raw = _mapping(top.get("groups", {}), "inventory.groups")
    groups: dict[str, tuple[str, ...]] = {}
    for group, members in groups_raw.items():
        if not isinstance(group, str) or not _ID.fullmatch(group) or group in hosts:
            raise ManifestError(f"invalid or conflicting group: {group!r}")
        if not isinstance(members, list) or not members:
            raise ManifestError(f"group {group} must list at least one host")
        for member in members:
            if not isinstance(member, str) or member not in hosts:
                raise ManifestError(f"group {group} references unknown host {member!r}")
        groups[group] = tuple(dict.fromkeys(members))
    return Inventory(hosts, groups)
