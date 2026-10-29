from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from platformctl.cli import main
from platformctl.errors import ManifestError
from platformctl.inventory import Host, Inventory, load_inventory
from platformctl.manifest import load_manifest
from platformctl.orchestration import HostResult, Orchestrator, SSHTransport
from platformctl.planner import build_targets
from platformctl.remote_worker import WORKER


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


def test_unassigned_defaults_to_controller(tmp_path: Path):
    p, i = fixtures(tmp_path)
    p.write_text("schema: 1\nservices:\n  legacy:\n    version: 1.0.0\n")
    assert build_targets(load_manifest(p), load_inventory(i))[0].host.name == "H1"
    inv = Inventory({"H2": Host("H2", "ssh", "10.0.0.2")}, {})
    host = build_targets(load_manifest(p), inv)[0].host
    assert (host.name, host.connection) == ("localhost", "local")


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


def test_local_executor_uses_local_engine_without_ssh(tmp_path: Path):
    p, i = fixtures(tmp_path)
    root = tmp_path / "app"
    (root / "1.0.0").mkdir(parents=True)
    p.write_text(
        "schema: 1\nservices:\n  app:\n    hosts: H1\n"
        f"    version: 1.0.0\n    root: {root}\n"
    )
    class NeverSSH(SSHTransport):
        def execute(self, *args, **kwargs):
            raise AssertionError("local must not SSH")
    stack = load_manifest(p)
    results = Orchestrator(NeverSSH()).execute(
        stack, build_targets(stack, load_inventory(i)), "check"
    )
    assert len(results) == 1 and results[0].code == 0
    assert "READY" in results[0].output


class FakeSSH(SSHTransport):
    def __init__(self, ready=False):
        self.uploads = {}
        self.downloaded = []
        self.readbacks = []
        self.executed = []
        self.cleaned = []
        self.ready = ready

    def workspace(self, host):
        return "/tmp/platformctl-12345678"

    def put(self, host, file, destination):
        self.uploads[destination] = file.read_bytes()

    def get(self, host, source, destination):
        self.readbacks.append(source)
        destination.write_bytes(b"artifact-content")

    def download(self, host, url, destination):
        self.downloaded.append((url, destination))

    def require(self, host, argv, file=None, **kwargs):
        if argv == ["dpkg", "--print-architecture"]:
            return "amd64"
        raise AssertionError(f"unexpected remote command: {argv}")

    def execute(self, host, argv, *, privileged=False):
        self.executed.append((host.name, list(argv), privileged))
        if argv[0] == "check" and not self.ready:
            return HostResult(host.name, "check", 2, "", "not installed")
        return HostResult(host.name, argv[0], 0, f"{argv[1]}: OK")

    def cleanup(self, host, workspace):
        self.cleaned.append((host.name, workspace))

    def run(self, host, argv, **kwargs):
        if argv == ["sudo", "-n", "bash", "-s", "--"]:
            return HostResult(host.name, "sudo-preflight", 0, "OK")
        raise AssertionError(f"unexpected run: {argv}")


def one_remote(tmp_path: Path, spec_text: str):
    _, i = fixtures(tmp_path)
    p = tmp_path / "stack.yaml"
    p.write_text("schema: 1\nservices:\n  C:\n    hosts: H2\n    version: 1.0.0\n" + spec_text)
    stack = load_manifest(p)
    return stack, build_targets(stack, load_inventory(i))


def test_remote_install_uploads_local_deb_and_checks_package(tmp_path: Path, monkeypatch):
    deb = tmp_path / "C-1.0.0.deb"
    deb.write_bytes(b"fake-package")
    unit = tmp_path / "C.service"
    unit.write_text("[Service]\nExecStart=/opt/C/current/bin/C\n")
    stack, targets = one_remote(
        tmp_path, f"    source: {deb}\n    service_file: ./C.service\n"
    )
    fake = FakeSSH()
    controller = Orchestrator(fake)
    inspections = []
    monkeypatch.setattr(
        controller.packages, "_inspect",
        lambda spec, source, folder, target_arch=None:
            inspections.append((source.read_bytes(), target_arch)),
    )
    result = controller.execute(stack, targets, "install")
    assert result[0].code == 0
    assert fake.uploads["/tmp/platformctl-12345678/C.deb"] == b"fake-package"
    assert fake.uploads["/tmp/platformctl-12345678/C.service"] == unit.read_bytes()
    assert inspections == [(b"fake-package", "amd64")]
    assert fake.executed[-1][1][0] == "install"
    assert fake.executed[-1][2] is True
    assert len(fake.executed[-1][1][-1]) == 64
    assert fake.cleaned == [("H2", "/tmp/platformctl-12345678")]
    assert all(cmd[1][0] in {"check", "install"} for cmd in fake.executed)


def test_remote_http_downloads_at_target_then_controller_validates(tmp_path: Path, monkeypatch):
    stack, targets = one_remote(
        tmp_path, "    source: https://nexus.example/C/1.0.0/C-1.0.0.deb\n"
    )
    fake = FakeSSH()
    controller = Orchestrator(fake)
    inspected = []
    monkeypatch.setattr(
        controller.packages, "_inspect",
        lambda spec, source, scratch, target_arch=None:
            inspected.append((source.read_bytes(), target_arch)),
    )
    result = controller.execute(stack, targets, "install")
    assert result[0].code == 0
    assert fake.downloaded == [
        ("https://nexus.example/C/1.0.0/C-1.0.0.deb", "/tmp/platformctl-12345678/C.deb")
    ]
    assert fake.readbacks == ["/tmp/platformctl-12345678/C.deb"]
    assert inspected == [(b"artifact-content", "amd64")]
    assert fake.executed[-1][2] is True


def test_target_local_deb_is_read_back_not_uploaded(tmp_path: Path, monkeypatch):
    stack, targets = one_remote(
        tmp_path, "    source: /mnt/shared/C-1.0.0.deb\n    source_location: target\n"
    )
    fake = FakeSSH()
    controller = Orchestrator(fake)
    monkeypatch.setattr(controller.packages, "_inspect", lambda *a, **kw: None)
    result = controller.execute(stack, targets, "install")
    assert result[0].code == 0
    assert fake.readbacks == ["/mnt/shared/C-1.0.0.deb"]
    assert fake.uploads == {}


def test_remote_check_does_not_transfer_artifact(tmp_path: Path):
    stack, targets = one_remote(tmp_path, "    source: /tmp/C.deb\n")
    fake = FakeSSH(ready=True)
    result = Orchestrator(fake).execute(stack, targets, "check")
    assert result[0].code == 0
    assert fake.uploads == {} and fake.readbacks == []


def test_remote_desktop_preserves_launcher_id(tmp_path: Path):
    launcher = tmp_path / "Briefcase.desktop"
    launcher.write_text("[Desktop Entry]\nType=Application\nName=Briefcase\nExec=briefcase\n")
    stack, targets = one_remote(
        tmp_path, "    type: desktop\n    desktop_file: ./Briefcase.desktop\n"
    )
    fake = FakeSSH(ready=True)
    result = Orchestrator(fake).execute(stack, targets, "status")
    assert result[0].code == 0
    assert fake.uploads["/tmp/platformctl-12345678/Briefcase.desktop"] == launcher.read_bytes()
    assert fake.executed[0][1][10].endswith("/Briefcase.desktop")


def test_apply_readiness_and_sudo_preflight(tmp_path: Path):
    p, i = fixtures(tmp_path)
    stack = load_manifest(p)
    targets = build_targets(stack, load_inventory(i))

    class OrchestratorWithFake(Orchestrator):
        def __init__(self):
            self.calls = []
            self.fail = True
        def _host(self, stack, target, command, software=None):
            self.calls.append((target.host.name, command))
            return HostResult(
                target.host.name, command,
                2 if self.fail and command == "check" and target.host.name == "H2" else 0,
                "READY",
            )
        def _privilege_check(self, target):
            self.calls.append((target.host.name, "sudo-preflight"))
            return HostResult(target.host.name, "sudo-preflight", 0, "OK")

    orch = OrchestratorWithFake()
    assert all(r.code for r in orch.execute(stack, targets, "apply", parallel=2))
    assert not any(command == "apply" for _, command in orch.calls)
    orch.calls.clear()
    orch.fail = False
    assert all(r.code == 0 for r in orch.execute(stack, targets, "apply"))
    assert [cmd for _, cmd in orch.calls].count("apply") == 3
    assert [cmd for _, cmd in orch.calls].count("sudo-preflight") == 3


def test_cli_plan_validate_and_legacy_local_path(tmp_path: Path, capsys):
    p, i = fixtures(tmp_path)
    assert main(["plan", "-f", str(p), "-i", str(i), "--limit", "H1,H3"]) == 0
    output = capsys.readouterr().out
    assert "H1" in output and "H3" in output and "H2" not in output
    assert main(["-i", str(i), "validate", "-f", str(p)]) == 0
    assert "3 host(s)" in capsys.readouterr().out
    assert main(["validate", "-f", str(p)]) == 2
    assert "requires --inventory" in capsys.readouterr().err


def test_bash_worker_is_syntactically_valid_and_runs_check_without_python(tmp_path: Path):
    assert subprocess.run(
        ["bash", "-n"], input=WORKER, text=True, capture_output=True
    ).returncode == 0
    root = tmp_path / "software"
    (root / "1.0.0").mkdir(parents=True)
    args = [
        "bash", "-s", "--", "check", "A", "1.0.0", "service",
        str(root), "1", "A.service", "running", "true",
        "", "", "", "", "0", "",
    ]
    process = subprocess.run(
        args, input=WORKER, text=True, capture_output=True
    )
    assert process.returncode == 0, process.stderr
    assert "A READY 1.0.0" in process.stdout
    assert "platformctl" not in process.stderr


def test_ssh_transport_does_not_disable_host_key_checks():
    args = SSHTransport()._ssh(Host("H2", "ssh", "server.example", "deploy", 2222))
    assert "StrictHostKeyChecking=yes" in args
    assert "BatchMode=yes" in args


def test_bash_worker_real_release_activation_and_rollback(tmp_path: Path):
    """Exercise symlink switch and rollback without SSH, sudo or systemd host."""
    root = tmp_path / "software"
    (root / "1.0.0").mkdir(parents=True)
    (root / "2.0.0").mkdir()
    (root / "current").symlink_to(root / "1.0.0")
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    systemctl = fakebin / "systemctl"
    systemctl.write_text(
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        "  is-active) exit 1 ;;\n"
        "  is-enabled) exit 1 ;;\n"
        "  start|stop|enable|disable|daemon-reload) exit 0 ;;\n"
        "  *) exit 8 ;;\n"
        "esac\n"
    )
    systemctl.chmod(0o755)
    import os
    env = {**os.environ, "PATH": str(fakebin) + ":" + os.environ["PATH"]}
    base = ["A", "2.0.0", "service", str(root), "1",
            "A.service", "stopped", "false", "", "", "", "", "0", ""]

    def run(action: str):
        return subprocess.run(
            ["bash", "-s", "--", action, *base],
            input=WORKER, text=True, capture_output=True, env=env,
        )

    applied = run("apply")
    assert applied.returncode == 0, applied.stderr
    assert (root / "current").resolve() == root / "2.0.0"
    assert (root / "previous").resolve() == root / "1.0.0"

    # A broken/new desired version must not block recovery to previous.
    base[1] = "9.9.9"
    rolled_back = run("rollback")
    assert rolled_back.returncode == 0, rolled_back.stderr
    assert (root / "current").resolve() == root / "1.0.0"
    assert (root / "previous").resolve() == root / "2.0.0"


def test_bash_worker_rejects_unmanaged_current_link(tmp_path: Path):
    root = tmp_path / "software"
    (root / "1.0.0").mkdir(parents=True)
    (root / "current").write_text("not a symlink")
    argv = [
        "bash", "-s", "--", "check", "A", "1.0.0", "service",
        str(root), "1", "A.service", "stopped", "false",
        "", "", "", "", "0", "",
    ]
    result = subprocess.run(argv, input=WORKER, text=True, capture_output=True)
    assert result.returncode != 0
    assert "not a symlink" in result.stderr


def test_bash_worker_refuses_modified_artifact_before_dpkg(tmp_path: Path):
    root = tmp_path / "software"
    root.mkdir()
    artifact = tmp_path / "artifact.deb"
    artifact.write_bytes(b"tampered-package")
    argv = [
        "bash", "-s", "--", "install", "A", "1.0.0", "service",
        str(root), "1", "A.service", "stopped", "false",
        "", "", str(artifact), "a-release-1.0.0", "1", "0" * 64,
    ]
    result = subprocess.run(argv, input=WORKER, text=True, capture_output=True)
    assert result.returncode != 0
    assert "SHA-256 changed after validation" in result.stderr
    assert not (root / "1.0.0").exists()
