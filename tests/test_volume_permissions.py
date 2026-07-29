from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.volume_permissions import secure_persistent_tree


def test_secure_persistent_tree_repairs_owner_only_modes(tmp_path: Path) -> None:
    nested = tmp_path / "appdata" / "igpsport"
    nested.mkdir(parents=True)
    credential = nested / "profile.json"
    credential.write_text('{"password":"secret"}', encoding="utf-8")
    nested.chmod(0o777)
    credential.chmod(0o666)

    secure_persistent_tree(tmp_path / "appdata", os.getuid(), os.getgid())

    assert (tmp_path / "appdata").stat().st_mode & 0o777 == 0o700
    assert nested.stat().st_mode & 0o777 == 0o700
    assert credential.stat().st_mode & 0o777 == 0o600


def test_secure_persistent_tree_rejects_symlinks(tmp_path: Path) -> None:
    persistent_root = tmp_path / "data"
    persistent_root.mkdir()
    (persistent_root / "outside").symlink_to(tmp_path)

    with pytest.raises(RuntimeError, match="Symlinks are not allowed"):
        secure_persistent_tree(persistent_root, os.getuid(), os.getgid())
