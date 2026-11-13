"""One place where a file name is joined onto a folder.

child(base, name) returns the real path of `name` inside `base`, or raises PathError.
It refuses names that contain a path separator, '..', a drive or a NUL byte, resolves symlinks,
and then checks that the resolved path is still inside the resolved base folder."""
from __future__ import annotations

import os


class PathError(Exception):
    """A file name would resolve outside the folder it is meant to stay in."""


def child(base: str, name: str) -> str:
    if not isinstance(name, str) or not name or "\x00" in name:
        raise PathError(f"Invalid file name: {name!r}")
    if name in (".", "..") or os.path.basename(name) != name or os.sep in name or (os.altsep and os.altsep in name):
        raise PathError(f"File name '{name}' must be a plain name with no folders")
    base_real = os.path.realpath(base)
    target = os.path.realpath(os.path.join(base_real, name))
    if os.path.commonpath([base_real, target]) != base_real:
        raise PathError(f"File name '{name}' resolves outside '{base}'")
    return target
