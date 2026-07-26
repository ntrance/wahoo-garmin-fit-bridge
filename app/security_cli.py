from __future__ import annotations

import getpass

from app.security import make_password_hash


def main() -> None:
    password = getpass.getpass("Password to hash: ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        raise SystemExit("Passwords did not match")
    print(make_password_hash(password))

