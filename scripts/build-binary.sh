#!/usr/bin/env bash
set -euo pipefail

# Build a self-contained (not fully static) Linux executable.
# Use a Linux build host compatible with the oldest supported target.
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

if ! command -v uv >/dev/null 2>&1; then
    echo "error: uv is required on the build machine" >&2
    exit 1
fi

uv sync --python 3.12 --group build

uv run --no-sync pyinstaller \
    --onefile \
    --clean \
    --noconfirm \
    --name platformctl \
    --distpath dist \
    --workpath build/pyinstaller \
    --specpath build/pyinstaller \
    scripts/entrypoint.py

./dist/platformctl --version
./dist/platformctl --help >/dev/null

(
    cd dist
    sha256sum platformctl > platformctl.sha256
)
printf 'Built executable: %s/dist/platformctl\n' "$repo_root"
