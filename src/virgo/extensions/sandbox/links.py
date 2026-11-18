"""Writable-path hardlink checks shared by the native and broker boundaries."""
from __future__ import annotations

import os
import stat
from collections import Counter
from pathlib import Path


def external_writable_links(roots: tuple[Path, ...], protected: tuple[Path, ...]) -> dict[tuple[int, int], Path]:
    """Return writable inodes with at least one link outside the writable roots.

    The returned mapping is ``(st_dev, st_ino) -> one writable pathname``. A
    file with all links inside the scanned writable roots is safe to write:
    every alias is already in the same writable scope. Counting is a launch
    check, not an atomic snapshot against trusted host writers.
    """
    def readonly(path: Path) -> bool:
        return any(path.is_relative_to(root) for root in protected)

    counts: Counter[tuple[int, int]] = Counter()
    links: dict[tuple[int, int], int] = {}
    paths: dict[tuple[int, int], Path] = {}
    seen: set[tuple[int, int, str]] = set()

    def record(path: Path, info: os.stat_result) -> None:
        if info.st_nlink == 1:
            return
        key = (info.st_dev, info.st_ino)
        marker = (info.st_dev, info.st_ino, str(path))
        if marker in seen:
            return
        seen.add(marker)
        counts[key] += 1
        links[key] = info.st_nlink
        paths.setdefault(key, path)

    def failed(error: OSError) -> None:
        raise error

    def scan(root: Path) -> None:
        root = Path(root)
        try:
            info = root.stat(follow_symlinks=False)
        except FileNotFoundError:
            return
        if stat.S_ISREG(info.st_mode):
            if not readonly(root):
                record(root, info)
            return
        if not stat.S_ISDIR(info.st_mode):
            return
        if hasattr(os, "fwalk"):
            for parent, directories, files, descriptor in os.fwalk(root, onerror=failed):
                parent = Path(parent)
                directories[:] = [name for name in directories if not readonly(parent / name)]
                for name in files:
                    path = parent / name
                    if readonly(path):
                        continue
                    try:
                        entry = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    if stat.S_ISREG(entry.st_mode):
                        record(path, entry)
            return
        for parent, directories, files in os.walk(root, followlinks=False):
            parent = Path(parent)
            directories[:] = [name for name in directories
                              if not readonly(parent / name) and not (parent / name).is_symlink()]
            for name in files:
                path = parent / name
                if readonly(path):
                    continue
                try:
                    entry = path.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if stat.S_ISREG(entry.st_mode):
                    record(path, entry)

    for root in roots:
        scan(root)
    return {key: paths[key] for key, count in counts.items()
            if links[key] > count}


def has_external_writable_link(path: Path, roots: tuple[Path, ...], protected: tuple[Path, ...]) -> bool:
    """Check one path without requiring a successful pre-scan of every root."""
    try:
        info = Path(path).stat(follow_symlinks=False)
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode) or info.st_nlink == 1:
        return False
    key = (info.st_dev, info.st_ino)
    return key in external_writable_links(roots, protected)
