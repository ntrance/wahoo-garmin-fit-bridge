from __future__ import annotations

import getpass
import os
import sys

from app.garmin_profile import garmin_token_dir
from app.settings import Settings


def main(settings: Settings | None = None) -> int:
    settings = settings or Settings.from_env()
    token_dir = garmin_token_dir(settings)
    token_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 50)
    print("   FIT to Garmin Bridge - Garmin Connect Login   ")
    print("=" * 50)
    print(f"Token storage directory: {token_dir}\n")

    email = os.environ.get("GARMIN_USERNAME", "").strip()
    if not email:
        email = input("Enter Garmin Account Email: ").strip()

    if not email:
        print("Error: Email cannot be empty.", file=sys.stderr)
        return 1

    password = os.environ.get("GARMIN_PASSWORD", "")
    if not password:
        password = getpass.getpass("Enter Garmin Account Password: ")

    if not password:
        print("Error: Password cannot be empty.", file=sys.stderr)
        return 1

    print("\nConnecting to Garmin Connect...")
    try:
        from garminconnect import Garmin

        def _prompt_mfa() -> str:
            print("\n🔐 Two-Factor Authentication (MFA) Required!")
            print(f"A verification code was sent to your Garmin email/phone for {email}.")
            code = input("Enter the one-time code: ").strip()
            return code

        client = Garmin(
            email=email,
            password=password,
            prompt_mfa=_prompt_mfa,
        )
        client.login(tokenstore=str(token_dir))
        print("\n✅ Login successful!")
        print(f"Garmin session tokens saved to: {token_dir}")
        print("The bridge will now automatically reuse these tokens for uploads without prompting.\n")
        return 0
    except Exception as exc:
        print(f"\n❌ Garmin login failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
