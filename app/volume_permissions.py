from __future__ import annotations

import os
from pathlib import Path

BRIDGE_UID = 10001
BRIDGE_GID = 10001
PERSISTENT_ROOTS = (Path("/data"), Path("/appdata"))


def secure_persistent_tree(path: Path, uid: int, gid: int) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise RuntimeError(f"Persistent root must not be a symlink: {path}")

    os.chown(path, uid, gid)
    path.chmod(0o700)

    for root, directories, files in os.walk(path, topdown=True, followlinks=False):
        root_path = Path(root)
        for name in directories:
            child = root_path / name
            if child.is_symlink():
                raise RuntimeError(f"Symlinks are not allowed in persistent storage: {child}")
            os.chown(child, uid, gid)
            child.chmod(0o700)
        for name in files:
            child = root_path / name
            if child.is_symlink():
                raise RuntimeError(f"Symlinks are not allowed in persistent storage: {child}")
            os.chown(child, uid, gid)
            child.chmod(0o600)


def main() -> None:
    if os.geteuid() != 0:
        raise RuntimeError("Volume permission initialization must run as root")
    for root in PERSISTENT_ROOTS:
        secure_persistent_tree(root, BRIDGE_UID, BRIDGE_GID)


if __name__ == "__main__":
    main()
