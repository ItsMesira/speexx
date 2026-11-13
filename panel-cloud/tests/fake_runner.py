"""Stand-in for runner.py, used by the integration test.

It speaks the same stdout contract and the same exit codes, so the panel's whole
supervision path can be exercised without a browser and without sending a single
request to the Speexx portal. A live login attempt against a real account is
exactly what must not happen by accident: repeated failures are what get an IP
locked.

Behaviour is derived from the password so the panel can drive every path
without extra flags: a password containing "wrong" fails the login exactly as a
real wrong password would, anything containing "hang" logs in and then sleeps so
there is something for stop to kill, and anything else runs the whole level.
"""

from __future__ import annotations

import argparse
import sys
import time

EXIT_LOGIN_FATAL = 3


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--credentials-stdin", action="store_true")
    ap.add_argument("--max-levels", type=int, default=1)
    ap.add_argument("--max-seconds", type=float, default=10800.0)
    ap.add_argument("--device-profile", type=int, default=0)
    ap.add_argument("--fast", action="store_true")
    args, _unknown = ap.parse_known_args()

    # Consume credentials exactly as the real runner does, then drop them.
    data = sys.stdin.read().split("\n", 1)
    email = data[0].strip() if data else ""
    password = data[1].rstrip("\n") if len(data) > 1 else ""
    if not email or not password:
        print("LOGIN_FAILED reason=unknown")
        return EXIT_LOGIN_FATAL

    if "wrong" in password:
        print("submitted login for %s" % email)
        print("LOGIN_FAILED reason=bad_credentials")
        print("refusing to retry; stopping to protect the IP")
        return EXIT_LOGIN_FATAL

    print("opened ephemeral context (device profile %d)" % args.device_profile)
    print("login form detected")
    print("LOGIN_OK verified=stub")

    if "hang" in password:
        while True:
            time.sleep(1)

    # A realistic level holds many packets; 12 keeps the fixture quick while
    # still being clearly more than one.
    packets_per_level = 12
    total = 0
    for level in range(1, args.max_levels + 1):
        for n in range(1, packets_per_level + 1):
            total += 1
            print("PACKET_DONE n=%d packet=%d level_packets=%d"
                  % (n, n, n))
        print("LEVEL_DONE n=%d packets=%d" % (level, packets_per_level))
    print("JOB_DONE levels=%d packets=%d" % (args.max_levels, total))
    return 0


if __name__ == "__main__":
    sys.exit(main())
