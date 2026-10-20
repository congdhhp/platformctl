# platformctl

`platformctl` is a small deployment controller for native Linux applications that must
run directly on the host (for example applications accessing GPIO, serial, I2C,
SPI, PCIe or other hardware).

It deliberately does **not** replace systemd and does **not** install build artifacts.
CI/CD deploys immutable versions to the host first; `platformctl` chooses which
deployed version is active and reconciles the systemd unit.

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

The systemd unit must execute via the stable `current` symlink:

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
    unit: softA.service
    state: running
    autostart: true

  softB:
    version: 3.1.0-build73
    unit: softB.service
    state: running
    autostart: true
```

With no service-level `root`, the managed root defaults to
`<platform.root>/<service>`. In the example, `softB` is deployed under
`/opt/platform/softB/3.1.0-build73`, while `softA` is deployed under
`/opt/softA/2.5.5-build184`.

`enabled: true|false` is also supported as a shorthand for the common
combination of runtime state and boot autostart.

## Existing installations: install_path

For a pre-existing vendor/legacy installation without versioned releases,
specify `install_path` instead of `root`:

```yaml
services:
  legacyApp:
    version: 3.1.0
    install_path: /opt/vendor/legacyApp
    unit: legacyApp.service
    state: running
    autostart: true
```

`root` and `install_path` are mutually exclusive. `install_path` must be
an absolute path to an existing directory.

In external mode:

- `validate` checks that the installation directory exists.
- `apply` only reconciles systemd start/stop and boot enablement; it does
  **not** install files, replace symlinks or switch versions.
- `version` is declared metadata only. `status` reports the current version
  as `unknown`, and `diff` does not falsely report version drift.
- `rollback` and `list-releases` are not supported.
- The existing systemd unit must already point to the intended executable.
  `platformctl` does not inspect or rewrite `ExecStart`, or verify which
  binary a running external service is using.

## Commands

```bash
platformctl validate -f stack.yaml
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

## Installation for development

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
pytest
```

## Design boundaries

- **CI/CD / package deployment** installs immutable versions directly under
  `<root>/<version>` for managed services.
- **platformctl** activates an already deployed managed version, or reconciles
  only systemd state for an external installation.
- **systemd** owns process lifecycle, restart policy, boot integration and logs.
- Mutable application data/config should remain outside the immutable versions
  (for example `/etc/platform/<service>` and `/var/lib/platform/<service>`).

## MVP limitations

External installations do not support version verification, switching, or
rollback. The MVP also does not include Nexus downloads, health-check plugins,
dependency ordering, stack-wide all-or-nothing rollback, or a privileged
daemon/API.
