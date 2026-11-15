"""round 577: every fake TOTP secret written into a service test must be long enough for current pyotp (at least 112 bits = 14 bytes after base32
decoding). The old fake secret JBSWY3DPEHPK3PXP is only 80 bits; a pyotp release that enforces the minimum made 7 position-stocks tests fail in CI
(services/position-stocks-service/requirements.txt does not pin pyotp). Plain asserts or pytest; stdlib only."""
from __future__ import annotations

import base64
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# an upper-case base32 literal of 16+ characters assigned to / passed as a TOTP secret
_PAT = re.compile(r"""(?:TOTP_SECRET|totp_secret)[^\n"']{0,40}["']([A-Z2-7]{16,})["']""")
_BARE = re.compile(r"""["'](JBSWY3DPEHPK3PXP[A-Z2-7]*)["']""")


def _secrets():
    for p in sorted((ROOT / "services").rglob("tests/*.py")):
        t = p.read_text(encoding="utf-8", errors="replace")
        for m in list(_PAT.finditer(t)) + list(_BARE.finditer(t)):
            yield p, m.group(1)


def test_every_fake_totp_secret_is_at_least_112_bits():
    seen = list(_secrets())
    assert seen, "pattern found no fake TOTP secret at all: the test is looking in the wrong place"
    short = []
    for p, s in seen:
        pad = s + "=" * (-len(s) % 8)
        if len(base64.b32decode(pad, casefold=True)) < 14:
            short.append(f"{p.relative_to(ROOT)}: {s} ({len(base64.b32decode(pad, casefold=True)) * 8} bits)")
    assert not short, "fake TOTP secret(s) under 112 bits:\n" + "\n".join(short)


def test_the_check_itself_flags_the_old_80_bit_secret():
    pad = "JBSWY3DPEHPK3PXP"
    assert len(base64.b32decode(pad, casefold=True)) * 8 == 80


if __name__ == "__main__":
    test_every_fake_totp_secret_is_at_least_112_bits()
    test_the_check_itself_flags_the_old_80_bit_secret()
    print("ok 2")
