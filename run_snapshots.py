"""Entry point used to build Snapshots.exe (PyInstaller needs a plain script)."""

from snapshots.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
