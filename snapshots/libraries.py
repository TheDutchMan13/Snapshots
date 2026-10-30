"""The photo folders Snapshots reads: the main library, and any extra folders (other drives, say).

Items in the main library are stored with a path relative to it ("2024/IMG_1.jpg"). Items in an
extra folder are stored with their whole path in forward slashes ("D:/Phone/IMG_1.jpg"), which a
relative path can never look like. So every stored path names exactly one file, and moving the main
library keeps working as before.
"""

import os
from dataclasses import dataclass
from pathlib import Path

from .db import is_external


@dataclass
class Root:
    path: Path        # the folder on disk
    prefix: str       # what stored paths of items in it start with ("" for the main library)
    main: bool

    @property
    def name(self) -> str:
        return self.path.name or str(self.path).rstrip("\\/") or str(self.path)

    def available(self) -> bool:
        try:
            return self.path.is_dir()
        except OSError:
            return False


def prefix_for(folder: Path) -> str:
    """The stored-path prefix of an extra folder: its absolute path with forward slashes, ending in /."""
    text = Path(os.path.abspath(folder)).as_posix()
    return text if text.endswith("/") else text + "/"


def _norm(p: Path) -> str:
    return os.path.normcase(os.path.abspath(p))


def _inside(child: Path, parent: Path) -> bool:
    c, p = _norm(child), _norm(parent)
    return c == p or c.startswith(p.rstrip("\\/") + os.sep)


def roots(cfg) -> list[Root]:
    """The main library first, then the extra folders (leaving out any inside another one)."""
    out = []
    if cfg.library_path is not None:
        out.append(Root(Path(cfg.library_path), "", True))
    for extra in cfg.extra_libraries:
        extra = Path(extra)
        if any(_inside(extra, r.path) or _inside(r.path, extra) for r in out):
            continue  # overlapping folders would list the same files twice
        out.append(Root(extra, prefix_for(extra), False))
    return out


def problem_with(cfg, folder: Path) -> str | None:
    """Why a folder can't be added as an extra library, or None."""
    if not folder.is_absolute():
        return "Please give the whole path of the folder, like D:\\Photos."
    if not folder.is_dir():
        return "That folder doesn't exist, or the drive isn't connected."
    for r in roots(cfg):
        if _inside(folder, r.path) or _inside(r.path, folder):
            return f"That folder overlaps {r.path}, which is already in the library."
    if _inside(folder, cfg.data_dir) or _inside(cfg.data_dir, folder):
        return "That folder holds Snapshots' own data folder."
    return None


def root_of(cfg, stored: str) -> Root | None:
    """The folder an item belongs to, or None if it belongs to a folder no longer in the settings."""
    for r in roots(cfg):
        if r.main:
            if not is_external(stored):
                return r
        elif stored.startswith(r.prefix):
            return r
    return None


def full_path(cfg, stored: str) -> Path | None:
    """Where an item's file is on disk (None if its folder isn't in the settings any more)."""
    r = root_of(cfg, stored)
    if r is None:
        return None
    rel = stored[len(r.prefix):]
    base = Path(os.path.normpath(r.path))
    full = Path(os.path.normpath(base / rel))
    if not full.is_relative_to(base):  # defence in depth: paths come from our own scan
        return None
    return full


def stored_path(root: Root, full: Path) -> str:
    rel = os.path.relpath(full, root.path).replace(os.sep, "/")
    return root.prefix + rel
