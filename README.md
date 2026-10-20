# platformctl

`platformctl` is a small deployment controller for native Linux applications that must run directly on the host (for example applications that access GPIO, serial, I2C, SPI, PCIe or other hardware).

It deliberately does **not** replace systemd and does **not** install build artifacts. CI/CD deploys immutable releases to the host first; `platformctl` only selects which deployed release is active and reconciles the corresponding systemd service.

## Filesystem model

```text
/opt/platform/
└── softA/
    ├── releases/
    │   ├── 2.5.4-build178/
    │   ├── 2.5.5-build184/
    │   └── 2.5.6-build191/
    ├── current  -> releases/2.5.5-build184/
    └── previous -> releases/2.5.4-build178/
```

The systemd unit should execute through `current`:

```ini
ExecStart=/opt/platform/softA/current/bin/softA
```

## Example stack.yaml

```yaml
schema: 1
platform:
  root: /opt/platform

services:
  softA:
    version: 2.5.5-build184
    unit: softA.service
    state: running
    autostart: true
```

`enabled: true|false` is also supported as a shorthand for the common combination of runtime state and boot autostart.

### Installation locations: managed or external

The default **managed** layout for each service is `<platform.root>/<service>/` with
`releases/<version>/`, `current`, and `previous`. Override only the root when a
service needs a different managed location:

```yaml
services:
  softA:
    version: 2.5.5-build184
    root: /srv/native/softA
    unit: softA.service
```

For a **pre-existing external/legacy installation** without versioned releases, use
`install_path` instead:

```yaml
services:
  legacyApp:
    version: 3.1.0
    install_path: /opt/vendor/legacyApp
    unit: legacyApp.service
    state: running
    autostart: true
```

`root` and `install_path` are mutually exclusive. `install_path` must be an
absolute path to an existing directory. In external mode:

- `validate` checks the installation directory exists.
- `apply` **only** reconciles systemd start/stop and boot enablement; it does
  not install files, replace symlinks, change executables or switch versions.
- `version` is **declared metadata only**: `status` displays the current
  version as `unknown` and `diff` notes that version drift cannot be verified.
- `rollback` and `list-releases` explicitly reject external installations.
- The existing systemd unit **must already point to the correct executable**
  in the external location. `platformctl` does not inspect or rewrite
  `ExecStart`, and it cannot prove the running process uses `install_path`.
  Version verification and rollback require a separate integration/strategy.

Do not put `releases` paths in the manifest. That is an internal layout used only
for managed installations.


## Commands

```bash
platformctl validate -f stack.yaml
platformctl status -f stack.yaml
platformctl diff -f stack.yaml
sudo platformctl apply -f stack.yaml
sudo platformctl rollback softA -f stack.yaml
platformctl list-releases softA -f stack.yaml
```

### `apply`

For each service, `platformctl`:

1. verifies that `/opt/platform/<service>/releases/<version>` exists;
2. stops the currently-running service when switching version;
3. atomically updates the `current` symlink and records `previous`;
4. enables/disables boot autostart according to the manifest;
5. starts the desired service;
6. verifies that systemd reports the unit active;
7. restores the previous symlink and service if activation fails.

## Installation for development

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
pytest
```

## Design boundaries

- **CI/CD / package deployment** installs immutable releases under `releases/`.
- **platformctl** selects and activates a deployed release.
- **systemd** owns process lifecycle, restart policy, boot integration and logs.
- Application state/config should remain outside the immutable release directory (for example `/etc/platform/<service>` and `/var/lib/platform/<service>`).

## MVP limitations

External installations do not support version verification, switching, or rollback. The first version intentionally does not include Nexus downloads, health-check plugins, dependency ordering between services, transactional all-or-nothing rollback across a whole stack, or a privileged daemon/API. Those can be layered on later without changing the release layout.
