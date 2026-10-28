"""PyInstaller entrypoint for the platformctl CLI."""

from platformctl.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
