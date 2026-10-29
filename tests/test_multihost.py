from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from platformctl.cli import main
from platformctl.errors import ManifestError
from platformctl.inventory import Host, Inventory, load_inventory
from platformctl.manifest import load_manifest
from platformctl.orchestration import HostResult, Orchestrator, SSHTransport, _remote_command
from platformctl.planner import build_targets


def fixtures(tmp_path: Path):
    stack = tmp_path / "stack.yaml"
    stack.write_text(
        "schema: 1\nservices:\n"
        "  A:\n    hosts: H1\n    version: 1.0.0\n"
        "  B:\n    hosts: H1\n    version: 1.0.0\n"
        "  C:\n    hosts: H2\n    version: 1.0.0\n"
        "  D:\n    hosts: H3\n    version: 1.0.0\n"
        "  E:\n    hosts: H3\n    version: 1.0.0\n"
    )
    inventory = tmp_path / "inventory.yaml"
    inventory.write_text(
        "defaults:\n  user: deploy\n  become: true\n"
        "hosts:\n  H1:\n    connection: local\n"
        "  H2:\n    address: 192.168.10.12\n"
        "  H3:\n    address: 192.168.10.13\n"
        "groups:\n  remote:\n    - H2\n    - H3\n"
    )
    return stack, inventory


def test_five_placements_three_hosts_and_limit(tmp_path: Path):
    p, i = fixtures(tmp_path)
    stack, inv = load_manifest(p), load_inventory(i)
    assert {t.host.name: [s.name for s in t.software] for t in build_targets(stack, inv)} == {
        "H1": ["A", "B"], "H2": ["C"], "H3": ["D", "E"],
    }
    assert [t.host.name for t in build_targets(stack, inv, "remote")] == ["H2", "H3"]
    assert [t.host.name for t in build_targets(stack, inv, "H1,H3")] == ["H1", "H3"]
    with pytest.raises(ManifestError):
        build_targets(stack, inv, "missing")


def test_one_software_multiple_hosts(tmp_path: Path):
    p, i = fixtures(tmp_path)
    p.write_text(p.read_text().replace("hosts: H2", "hosts: [H1, H2]"))
    targets = build_targets(load_manifest(p), load_inventory(i))
    assert [s.name for s in targets[0].software] == ["A", "B", "C"]
    assert [s.name for s in targets[1].software] == ["C"]


def test_unassigned_software_defaults_to_controller(tmp_path: Path):
    p, i = fixtures(tmp_path)
    p.write_text("schema: 1\nservices:\n  legacy:\n    version: 1.0.0\n")
    assert build_targets(load_manifest(p), load_inventory(i))[0].host.name == "H1"
    inv = Inventory({"H2": Host("H2", "ssh", "10.0.0.2")}, {})
    target = build_targets(load_manifest(p), inv)[0]
    assert (target.host.name, target.host.connection) == ("localhost", "local")


@pytest.mark.parametrize("bad", [
    "hosts:\n  H1:\n    connection: ssh\n",
    "hosts:\n  H1:\n    connection: local\n    address: 10.0.0.1\n",
    "hosts:\n  H1:\n    address: -malicious\n",
    "hosts:\n  H1:\n    address: 10.0.0.1\n    become: abc\n",
    "hosts:\n  H1:\n    address: 10.0.0.1\n    port: 70000\n",
    "hosts:\n  H1:\n    connection: local\n"
    "groups:\n  g:\n    - Unknown\n",
])
def test_invalid_inventory_rejected(tmp_path: Path, bad: str):
    p = tmp_path / "inventory.yaml"
    p.write_text(bad)
    with pytest.raises(ManifestError):
        load_inventory(p)


def test_local_hostname_binding_rejects_wrong_machine(tmp_path: Path):
    _, i = fixtures(tmp_path)
    i.write_text(i.read_text().replace(
        "connection: local\n", "connection: local\n    hostname: not-this-machine\n"
    ))
    with pytest.raises(ManifestError, match="requires hostname"):
        load_inventory(i)


def test_local_executor_runs_without_ssh(tmp_path: Path):
    p, i = fixtures(tmp_path)
    root = tmp_path / "app"
    (root / "1.0.0").mkdir(parents=True)
    p.write_text(
        "schema: 1\nservices:\n  app:\n    hosts: H1\n"
        f"    version: 1.0.0\n    root: {root}\n"
    )
    class NeverSSH(SSHTransport):
        def run(self, *args, **kwargs):
            raise AssertionError("local should never SSH")
    stack = load_manifest(p)
    result = Orchestrator(NeverSSH()).execute(
        stack, build_targets(stack, load_inventory(i)), "check"
    )
    assert result[0].code == 0 and "READY" in result[0].output


class FakeSSH(SSHTransport):
    def __init__(self):
        self.uploads = {}
        self.commands = []
        self.cleaned = []

    def require(self, host, argv, file=None):
        if argv == ["platformctl", "--version"]:
            return "platformctl 0.1.0"
        if argv[0] == "mktemp":
            return "/tmp/platformctl-12345678"
        raise AssertionError(f"unexpected SSH command: {argv}")

    def put(self, host, file, destination):
        self.uploads[destination] = file.read_bytes()

    def run(self, host, argv, file=None):
        self.commands.append(argv)
        return HostResult(host.name, argv[0], 0, "OK")

    def cleanup(self, host, workspace):
        self.cleaned.append(workspace)


def test_local_deb_and_service_file_stage_to_remote(tmp_path: Path):
    _, i = fixtures(tmp_path)
    deb = tmp_path / "A-1.0.0.deb"
    deb.write_bytes(b"package-data")
    unit = tmp_path / "A.service"
    unit.write_text("[Service]\nExecStart=/opt/A/current/bin/A\n")
    p = tmp_path / "stack.yaml"
    p.write_text(
        "schema: 1\nservices:\n  A:\n    hosts: H2\n    version: 1.0.0\n"
        f"    source: {deb}\n    service_file: ./A.service\n"
    )
    stack = load_manifest(p)
    fake = FakeSSH()
    results = Orchestrator(fake).execute(
        stack, build_targets(stack, load_inventory(i)), "install"
    )
    assert results[0].code == 0
    assert fake.uploads["/tmp/platformctl-12345678/artifact-A.deb"] == deb.read_bytes()
    assert fake.uploads["/tmp/platformctl-12345678/config-A.service"] == unit.read_bytes()
    effective = yaml.safe_load(fake.uploads["/tmp/platformctl-12345678/stack.yaml"])
    assert effective["services"]["A"]["source"].endswith("/artifact-A.deb")
    assert effective["services"]["A"]["service_file"].endswith("/config-A.service")
    assert fake.commands[0][:3] == ["sudo", "-n", "platformctl"]
    assert fake.cleaned == ["/tmp/platformctl-12345678"]


def test_remote_desktop_staging_preserves_desktop_id(tmp_path: Path):
    _, i = fixtures(tmp_path)
    launcher = tmp_path / "Briefcase.desktop"
    launcher.write_text("[Desktop Entry]\nType=Application\nName=Briefcase\nExec=briefcase\n")
    p = tmp_path / "stack.yaml"
    p.write_text(
        "schema: 1\nservices:\n  B:\n    hosts: H2\n    type: desktop\n"
        "    version: 2.0.0\n    desktop_file: ./Briefcase.desktop\n"
    )
    stack = load_manifest(p)
    fake = FakeSSH()
    result = Orchestrator(fake).execute(
        stack, build_targets(stack, load_inventory(i)), "status"
    )
    assert result[0].code == 0
    staged = "/tmp/platformctl-12345678/Briefcase.desktop"
    assert staged in fake.uploads
    effective = yaml.safe_load(fake.uploads["/tmp/platformctl-12345678/stack.yaml"])
    assert effective["services"]["B"]["desktop_file"] == staged


def test_remote_http_download_on_target_and_no_deb_upload(tmp_path: Path):
    _, i = fixtures(tmp_path)
    p = tmp_path / "stack.yaml"
    p.write_text(
        "schema: 1\nservices:\n  C:\n    hosts: H2\n    version: 1.0.0\n"
        "    source: https://nexus.example/C/1.0.0/C-1.0.0.deb\n"
    )
    stack = load_manifest(p)
    fake = FakeSSH()
    Orchestrator(fake).execute(stack, build_targets(stack, load_inventory(i)), "install")
    assert list(fake.uploads) == ["/tmp/platformctl-12345678/stack.yaml"]
    effective = yaml.safe_load(fake.uploads["/tmp/platformctl-12345678/stack.yaml"])
    assert effective["services"]["C"]["source"].startswith("https://")


def test_target_local_source_does_not_get_uploaded(tmp_path: Path):
    _, i = fixtures(tmp_path)
    p = tmp_path / "stack.yaml"
    p.write_text(
        "schema: 1\nservices:\n  C:\n    hosts: H2\n    version: 1.0.0\n"
        "    source: /mnt/shared/C-1.0.0.deb\n    source_location: target\n"
    )
    stack = load_manifest(p)
    fake = FakeSSH()
    Orchestrator(fake).execute(stack, build_targets(stack, load_inventory(i)), "install")
    assert list(fake.uploads) == ["/tmp/platformctl-12345678/stack.yaml"]
    effective = yaml.safe_load(fake.uploads["/tmp/platformctl-12345678/stack.yaml"])
    assert effective["services"]["C"]["source"] == "/mnt/shared/C-1.0.0.deb"


def test_check_does_not_transfer_local_deb(tmp_path: Path):
    _, i = fixtures(tmp_path)
    deb = tmp_path / "C.deb"
    deb.write_bytes(b"example")
    p = tmp_path / "stack.yaml"
    p.write_text(
        "schema: 1\nservices:\n  C:\n    hosts: H2\n"
        f"    version: 1.0.0\n    source: {deb}\n"
    )
    stack = load_manifest(p)
    fake = FakeSSH()
    Orchestrator(fake).execute(stack, build_targets(stack, load_inventory(i)), "check")
    assert "/tmp/platformctl-12345678/artifact-C.deb" not in fake.uploads


def test_apply_checks_every_host_before_activation(tmp_path: Path):
    p, i = fixtures(tmp_path)
    stack = load_manifest(p)
    targets = build_targets(stack, load_inventory(i))
    class FakeOrchestrator(Orchestrator):
        def __init__(self):
            self.calls = []
        def _host(self, stack, target, command, software=None):
            self.calls.append((target.host.name, command))
            if command == "check" and target.host.name == "H2":
                return HostResult("H2", command, 2, "", "not installed")
            return HostResult(target.host.name, command, 0, "READY")
    orch = FakeOrchestrator()
    results = orch.execute(stack, targets, "apply", parallel=2)
    assert len(results) == 3 and all(r.code != 0 for r in results)
    assert {command for _, command in orch.calls} == {"check"}


def test_cli_plan_validate_and_missing_inventory(tmp_path: Path, capsys):
    p, i = fixtures(tmp_path)
    assert main(["plan", "-f", str(p), "-i", str(i), "--limit", "H1,H3"]) == 0
    output = capsys.readouterr().out
    assert "H1" in output and "H3" in output and "H2" not in output
    assert main(["-i", str(i), "validate", "-f", str(p)]) == 0
    assert "3 host(s)" in capsys.readouterr().out
    assert main(["validate", "-f", str(p)]) == 2
    assert "requires --inventory" in capsys.readouterr().err


def test_target_relative_source_rejected(tmp_path: Path):
    p = tmp_path / "stack.yaml"
    p.write_text(
        "schema: 1\nservices:\n  C:\n    version: 1.0.0\n"
        "    source: ./C.deb\n    source_location: target\n"
    )
    with pytest.raises(ManifestError):
        load_manifest(p)


def test_ssh_is_strict_and_privilege_escalation_only_for_mutations():
    host = Host("H2", "ssh", "server.example", "deploy", 2222)
    args = SSHTransport()._ssh(host)
    assert "StrictHostKeyChecking=yes" in args and "BatchMode=yes" in args
    assert args[-1] == "server.example"
    assert _remote_command(host, "check", "/tmp/stack.yaml", None)[0] == "platformctl"
    assert _remote_command(host, "install", "/tmp/stack.yaml", None)[:3] == [
        "sudo", "-n", "platformctl",
    ]
