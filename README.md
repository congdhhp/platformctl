# platformctl

`platformctl` is a small deployment controller for native Linux applications that must
run directly on the host (for example applications accessing GPIO, serial, I2C,
SPI, PCIe or other hardware).

It deliberately does **not** replace systemd. CI/CD publishes immutable,
version-specific Debian packages or pre-deploys release directories. `platformctl`
can install a missing .deb in a separate preparation step, verify that desired
releases are ready, then activate them and reconcile systemd at deployment time.

## Filesystem model: flat managed layout

Managed installations use **`<root>/<version>/`** directly:

```text
/opt/softA/
├── 2.5.4-build178/
│   └── bin/softA
├── 2.5.5-build184/
│   └── bin/softA
├── current  -> 2.5.5-build184/
└── previous -> 2.5.4-build178/
```

A managed systemd unit should execute via the stable `current` symlink:

```ini
ExecStart=/opt/softA/current/bin/softA
```

`current` and `previous` are reserved names and cannot be used as version
identifiers. Version directories must be ordinary directories, **not symlinks**.
Keep immutable version directories and controller symlinks under `root`; place
mutable application data/config outside that directory.

### Example stack.yaml

```yaml
schema: 1
platform:
  root: /opt/platform

services:
  softA:
    version: 2.5.5-build184
    root: /opt/softA
    source: http://nexus.lan:8081/repository/native-releases/softA/
    service_name: softA.service
    service_file: ./examples/softA.service
    state: running
    autostart: true

  softB:
    version: 3.1.0-build73
    service_name: softB.service
    state: running
    autostart: true
```

With no service-level `root`, the managed root defaults to
`<platform.root>/<service>`. In the example, `softB` is deployed under
`/opt/platform/softB/3.1.0-build73`, while `softA` is deployed under
`/opt/softA/2.5.5-build184`.

`enabled: true|false` is also supported as a shorthand for the common
combination of runtime state and boot autostart.

## Optional source: install without activation

A managed service may specify a single optional `source` string:

```yaml
services:
  controller:
    version: 2.5.5-build184
    root: /opt/ControllerSoftware
    source: http://nexus.lan:8081/repository/native-releases/controller/
    service_name: controller.service
    service_file: ./services/controller.service
```

Supported source forms:

- **Direct .deb**: `https://nexus.lan/releases/controller.deb` or `/mnt/packages/controller.deb`.
- **Directory** (trailing slash required): `https://nexus.lan/releases/controller/` or `/mnt/packages/controller/`. The resolved artifact is exactly `<source>/<version>/<service>-<version>.deb`.
- Relative local paths are resolved from the `stack.yaml` directory.
- Nexus Raw repositories work as ordinary HTTP(S) sources. No Nexus search API or APT integration is required. HTTP without TLS provides **no transport integrity**; only use trusted internal sources. Remote authentication and signed/checksum-verified artifacts are not implemented yet.

`source` is optional; installations previously deployed by other means remain supported. It is not supported for the external `install_path` mode.

### Preparation workflow

```bash
platformctl validate -f stack.yaml
sudo platformctl install -f stack.yaml
platformctl check -f stack.yaml
# When the deployment is approved:
sudo platformctl apply -f stack.yaml
```

- `validate` checks the manifest and an optional local `service_file`, but **does not require a deployed release**.
- `install` resolves and downloads a missing .deb, inspects it, then executes `dpkg -i`. A valid already-installed release is skipped. It **does not** change `current`, `previous`, systemd units, enablement or running process state. `dpkg` may still have other OS-level side effects, so only trusted packages may be installed.
- `check` is read-only; it checks each desired managed directory and controller symlinks. With `source`, it also verifies the expected Debian package and exact Debian version are in the dpkg installed state. Without `source`, it accepts an existing version directory as a legacy/pre-deployed release. External `install_path` is reported as version unverified.
- `apply` **never downloads or installs**. It checks that desired releases are present, then updates `current` and reconciles systemd, including an optional `service_file`. An unprepared release fails before any service is touched.

### Mandatory versioned Debian package convention

For `controller` version `2.5.5-build184`:

```text
Filename: controller-2.5.5-build184.deb
Debian Package: controller-release-2.5.5-build184
Debian Version: 2.5.5-build184
Payload: /opt/ControllerSoftware/2.5.5-build184/...
```

The actual Debian **Package** field must be distinct per release; a different
filename or installation path alone is not sufficient for `dpkg` co-installation.
Package name is derived as `<service lowercase>-release-<version lowercase>`;
source-backed versions must use Debian-compatible lowercase identifiers.
The Debian **Version** field must equal the desired `version` in stack.yaml.

Preflight rejects packages whose payload includes files **outside the exact
version directory**, unsafe links, special files, or maintainer scripts such
as `postinst`, `preinst`, `prerm`, `postrm`, `config`, `triggers`.
This deliberately rejects legacy packages whose `postinst` creates or
restarts a shared service. Adjust CMake/CPack to produce a script-free,
version-specific **application release** .deb; configure the systemd unit
separately with `service_file` if required. Do not package `current` or
`previous` into the .deb.

The package manager uses `dpkg -i`, which does **not** automatically install
missing dependencies. Shared system libraries and dependencies should be
pre-provisioned. No permanent download cache, artifact checksum verification,
package cleanup, or filesystem transaction is provided yet. Installing a .deb
is not rolled back automatically if dpkg fails partway through. For production,
use a trusted artifact repository over HTTPS and independently verify the
release provenance before installation.


## Optional systemd service file

`service_name` replaces the former `unit` field. It defaults to
`<service>.service`, for example `controller.service`. The old `unit`
field is not accepted.

`service_file` is optional. It points to a **complete** systemd `.service`
file, resolved relative to the directory containing `stack.yaml`:

```yaml
services:
  controller:
    version: 2.5.5-build184
    root: /opt/ControllerSoftware
    service_name: controller.service
    service_file: ./services/controller.service
```

If omitted, platformctl leaves the installed unit file untouched and controls
its runtime state only. If present, `validate` checks the source file,
`diff` reports changes, and `apply` copies the file to
`/etc/systemd/system/<service_name>` when its bytes differ. It then issues
`systemctl daemon-reload` and restarts an already-running service, even if
the version is unchanged. Writes are atomic and the original unit is restored
on an apply failure, together with pre-apply release symlinks and service
state when possible.

The override path in `/etc/systemd/system` takes priority over a vendor unit
under `/usr/lib/systemd/system`. If a package already owns a file under
`/etc/systemd/system`, platformctl will overwrite that file when explicitly
configured with `service_file`. Package reinstall/upgrade may overwrite it
again; coordinate ownership and maintainer scripts before use.

**Limitations:** `service_file` is a full unit replacement, not a drop-in,
and only basic file validation is performed, not a full systemd syntax check.
The original unit is held for an in-process failed-apply restore, but no
persistent backup or ownership registry is maintained yet. Removing
`service_file` from a later manifest does **not** automatically restore a
previously overwritten unit. A manual `rollback` command changes the release
symlinks only, not the unit definition. The systemd unit should continue to
reference `<root>/current` across versions.

## Existing installations: install_path

For a pre-existing vendor/legacy installation without versioned releases,
specify `install_path` instead of `root`:

```yaml
services:
  legacyApp:
    version: 3.1.0
    install_path: /opt/vendor/legacyApp
    service_name: legacyApp.service
    state: running
    autostart: true
```

`root` and `install_path` are mutually exclusive. `install_path` must be
an absolute path to an existing directory.

In external mode:

- `check` verifies that the installation directory exists; `validate` checks only configuration.
- `apply` only reconciles systemd start/stop and boot enablement; it does
  **not** deploy application releases, change version symlinks or switch versions.
- `version` is declared metadata only. `status` reports the current version
  as `unknown`, and `diff` does not falsely report version drift.
- `rollback` and `list-releases` are not supported.
- The existing systemd unit must already point to the intended executable.
  `platformctl` does not inspect or rewrite `ExecStart`, or verify which
  binary a running external service is using.

## Commands

```bash
platformctl validate -f stack.yaml
sudo platformctl install -f stack.yaml
platformctl check -f stack.yaml
platformctl status -f stack.yaml
platformctl diff -f stack.yaml
sudo platformctl apply -f stack.yaml
sudo platformctl rollback softA -f stack.yaml
platformctl list-releases softA -f stack.yaml
```

### Managed apply

For each managed service, `platformctl`:

1. Checks that `<root>/<version>` is a deployed directory and that any existing
   `current` / `previous` symlinks point to direct version directories.
2. Stops the currently running service when switching versions.
3. Atomically updates `current` and records `previous`.
4. Enables/disables boot autostart according to the manifest.
5. Starts the desired service and checks whether systemd reports it active.
6. Attempts a per-service rollback if activation fails and an earlier version
   was recorded.

`list-releases` enumerates version directories directly under `root`;
the controller symlinks `current` and `previous` are excluded. Invalid
controller symlinks are rejected before `apply` changes a systemd service.

## Standalone Linux executable (PyInstaller)

In addition to the Python package (`uv tool install` / `pip`), `platformctl`
can be built as a **single-file Linux executable**, using PyInstaller.
The target host does not need Python, uv or PyYAML installed.

### Build on Linux (with uv)

Build on **Ubuntu 22.04 x86_64** for the current binary release baseline.
The build machine must have `uv` and access to Python dependencies:

```bash
bash scripts/build-binary.sh
```

The script installs the isolated `build` dependency group from
`pyproject.toml`, runs `pyinstaller --onefile`, performs `--version` and
`--help` smoke checks, and creates:

```text
dist/
├── platformctl
└── platformctl.sha256
```

To install on a **compatible Linux x86_64** target:

```bash
cd dist
sha256sum --check platformctl.sha256
sudo install -o root -g root -m 755 platformctl /usr/local/bin/platformctl
platformctl --version
```

On PRs, `.github/workflows/build-binary.yml` builds the executable on an
Ubuntu 22.04 x86_64 GitHub Actions runner, tests it against a real YAML
manifest, and uploads a workflow artifact. A push of a matching version tag
(for example `v0.1.0` for `pyproject.toml` version `0.1.0`) additionally
builds a Python wheel and publishes these GitHub Release assets:

```text
platformctl-v0.1.0-linux-x86_64
platformctl-v0.1.0-linux-x86_64.sha256
platformctl-0.1.0-py3-none-any.whl
```

Release assets from GitHub must be marked executable after download
(e.g. `chmod +x platformctl-v0.1.0-linux-x86_64`).

**Limitations:** PyInstaller `--onefile` bundles the Python interpreter but
does **not** make a fully statically linked ELF; it relies on compatible
Linux/glibc libraries and extracts runtime files to a temporary directory
(which must allow execution). Build/test separately for other CPU architectures
or older Linux baselines. `platformctl install` and `apply` still require
system tools (`dpkg`, `dpkg-deb`, `dpkg-query`, `systemctl`) and typically
root privileges. The binary build does not remove those dependencies.

## Installation for development

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
pytest
```

## Design boundaries

- **CI/CD / package deployment** publishes co-installable, version-specific
  .deb packages or pre-deploys immutable versions directly under
  `<root>/<version>` for managed services.
- **platformctl** acquires/installs requested releases during an explicit
  preparation step, then activates prepared versions or reconciles systemd
  state for external installations (and optionally its unit file).
- **systemd** owns process lifecycle, restart policy, boot integration and logs.
- Mutable application data/config should remain outside the immutable versions
  (for example `/etc/platform/<service>` and `/var/lib/platform/<service>`).

## MVP limitations

External installations do not support version verification, switching, or
rollback. The MVP also does not include a Nexus-specific client, health-check plugins,
dependency ordering, stack-wide all-or-nothing rollback, or a privileged
daemon/API.
