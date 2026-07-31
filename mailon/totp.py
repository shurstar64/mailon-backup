"""TOTP (RFC 6238) code generator.

Generates the SAME 6-digit code that Google Authenticator on your phone
shows at the same moment, because both use the same shared secret.

mailon.kr uses Google OTP (standard TOTP), so pyotp with default
parameters (SHA1, 6 digits, 30-second step) is compatible.
"""
from __future__ import annotations

import ctypes
import logging
import time

import pyotp


log = logging.getLogger(__name__)


def generate_code(secret: str, at: float | None = None) -> str:
    """Return current 6-digit TOTP code for the given Base32 secret.

    Args:
        secret: Base32-encoded TOTP secret (as shown when you set up Google OTP).
                Whitespace is stripped.
        at:     Optional Unix timestamp. Defaults to current time.

    Returns:
        6-character string of digits (may have leading zeros).
    """
    cleaned = secret.replace(" ", "").replace("-", "").strip()
    totp = pyotp.TOTP(cleaned)
    if at is None:
        return totp.now()
    return totp.at(int(at))


def seconds_until_next_code(at: float | None = None) -> int:
    """Seconds remaining until the current 30-second TOTP window rolls over."""
    now = at if at is not None else time.time()
    return 30 - int(now) % 30


def verify_code(secret: str, code: str, window: int = 1) -> bool:
    """Check whether a code matches for the secret (for debugging)."""
    cleaned = secret.replace(" ", "").replace("-", "").strip()
    return pyotp.TOTP(cleaned).verify(code, valid_window=window)


def check_system_clock_skew(max_drift_seconds: int = 10) -> float | None:
    """Warn if Windows time drifts too far from a public TOTP reference.

    TOTP is time-sensitive: if the machine clock is >30s off, codes fail.
    We ping Windows's own time service status for a quick sanity check
    and return the reported offset in seconds (or None on failure).

    This is advisory only - does NOT block login.
    """
    try:
        # Windows W32Time reports offset in its status output. Instead of
        # parsing localized output, we use the OS API if available.
        # Here we just report the local clock; actual sync status is a
        # Task Scheduler/OS responsibility.
        # For cron runs, a best-effort check is fine.
        import subprocess
        out = subprocess.run(
            ["w32tm", "/query", "/status"],
            capture_output=True, text=True, timeout=5,
        )
        for line in out.stdout.splitlines():
            # Look for "Last Successful Sync Time" / "마지막 동기화"
            if "offset" in line.lower() or "오프셋" in line:
                # Extract float seconds if present
                import re
                m = re.search(r"([-+]?\d+\.?\d*)", line)
                if m:
                    drift = float(m.group(1))
                    if abs(drift) > max_drift_seconds:
                        log.warning("system clock drift: %.2fs (>%ds threshold)",
                                    drift, max_drift_seconds)
                    return drift
    except Exception as e:
        log.debug("clock skew check skipped: %s", e)
    return None


if __name__ == "__main__":
    # Quick self-test: python -m mailon.totp <secret>
    import sys

    if len(sys.argv) < 2:
        print("Usage: python -m mailon.totp <BASE32_SECRET>")
        sys.exit(1)
    s = sys.argv[1]
    print(f"Code:  {generate_code(s)}")
    print(f"Valid for: {seconds_until_next_code()} more seconds")
