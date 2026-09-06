#!/usr/bin/env python3
"""
generate_secrets.py — run this LOCALLY (your laptop, not Render) to produce
the three real-trade-service secrets. Never commit the output, never paste
it anywhere except directly into Render's environment variable editor.

Why this is a script and not the "admin page" you asked for: the admin
password hash has to be produced by the SAME argon2-cffi library that
verifies it later (auth/admin_auth.py's PasswordHasher().verify()). A
browser-based Argon2 generator would need its own WASM implementation that
I have no way to test end-to-end against your actual backend in this
environment — shipping an unverified crypto tool for your real password
hash is exactly the kind of "looks done but untested" risk I don't want to
hand you for something this sensitive. The other two secrets (SESSION_SECRET,
DHAN_CREDENTIAL_ENC_KEY) ARE safe to generate in the browser — see the
setup-secrets.html artifact for those, and the process notes below for why
they're lower-risk.

Usage:
    cd services/real-trade-service
    pip install argon2-cffi cryptography python-dotenv --break-system-packages   # if not already installed
    python3 scripts/generate_secrets.py

You'll be prompted for your admin password (input is hidden, not echoed to
the terminal, and never written to disk). The script prints all three
values once; copy them straight into Render's dashboard.
"""
from __future__ import annotations

import base64
import getpass
import os
import secrets
import sys


def main() -> None:
    print("=" * 70)
    print("Stockky Real Automatic Trade — one-time secrets generator")
    print("Run this locally. Do not commit or paste the output anywhere")
    print("except Render's environment variable editor.")
    print("=" * 70)

    # 1. ADMIN_PASSWORD_HASH
    try:
        from argon2 import PasswordHasher
    except ImportError:
        print("\nERROR: argon2-cffi not installed. Run:")
        print("  pip install argon2-cffi --break-system-packages")
        sys.exit(1)

    pw1 = getpass.getpass("\nChoose your admin password (input hidden): ")
    pw2 = getpass.getpass("Confirm it: ")
    if pw1 != pw2:
        print("Passwords did not match — run the script again.")
        sys.exit(1)
    if len(pw1) < 12:
        print("WARNING: that's a short password for something guarding a real")
        print("brokerage connection. Consider 16+ characters / a passphrase.")
    admin_hash = PasswordHasher().hash(pw1)
    del pw1, pw2  # don't let it linger in memory longer than necessary

    # 2. SESSION_SECRET — 32 random bytes, base64. Just needs to be
    #    unguessable; it only signs short-lived session JWTs.
    session_secret = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()

    # 3. DHAN_CREDENTIAL_ENC_KEY — must be a valid Fernet key specifically
    #    (32 url-safe base64-encoded bytes), not just any random string, or
    #    auth/dhan_credentials.py's Fernet(...) constructor will reject it.
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        print("\nERROR: cryptography not installed. Run:")
        print("  pip install cryptography --break-system-packages")
        sys.exit(1)
    enc_key = Fernet.generate_key().decode()

    print("\n" + "=" * 70)
    print("COPY THESE INTO RENDER (real-trade-service → Environment):")
    print("=" * 70)
    print(f"ADMIN_PASSWORD_HASH={admin_hash}")
    print(f"SESSION_SECRET={session_secret}")
    print(f"DHAN_CREDENTIAL_ENC_KEY={enc_key}")
    print("=" * 70)
    print("\nADMIN_USERNAME defaults to 'admin' — set ADMIN_USERNAME too if you")
    print("want a different login name.")
    print("\nNothing above was written to disk. Close this terminal when done.")


if __name__ == "__main__":
    main()
