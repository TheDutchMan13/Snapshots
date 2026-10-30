"""Downloading model files once, and checking they are genuine."""

import hashlib
import os
import urllib.request
from pathlib import Path


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def missing(folder: Path, files: dict[str, dict]) -> list[str]:
    """Names of files that still have to be downloaded (absent; not checked against the hash)."""
    return [name for name in files if not (folder / name).is_file()]


def ensure_files(folder: Path, files: dict[str, dict], timeout: float = 300) -> dict[str, Path]:
    """Make sure every file is present and has the expected SHA-256, downloading it if not.

    files: {file name: {"sha256": ..., "urls": [...]}}. Returns {file name: path}.
    """
    folder.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name, spec in files.items():
        path = folder / name
        if path.is_file() and sha256_of(path) == spec["sha256"]:
            paths[name] = path
            continue
        last_error = None
        for url in spec["urls"]:
            tmp = path.with_name(path.name + ".part")
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Snapshots photo library"})
                with urllib.request.urlopen(req, timeout=timeout) as resp, open(tmp, "wb") as out:
                    while chunk := resp.read(1 << 16):
                        out.write(chunk)
                if sha256_of(tmp) != spec["sha256"]:
                    raise ValueError("the downloaded file did not match the expected checksum")
                os.replace(tmp, path)
                paths[name] = path
                break
            except Exception as exc:
                last_error = exc
                tmp.unlink(missing_ok=True)
        if name not in paths:
            raise RuntimeError(f"Could not download {name}: {last_error}")
    return paths
