#!/usr/bin/env python3
"""tunnel_ngrok.py — cloud fallback for tunnel.sh.

The macOS original execs the ngrok binary with an authtoken already in ngrok's
own config. On Google Colab / Kaggle there is no such binary or config, so this
script drives the tunnel through pyngrok instead. Needs:

  * pyngrok installed in the panel venv (pip install pyngrok), and
  * NGROK_AUTHTOKEN in the environment (ngrok dashboard -> Your Authtoken).

It writes the public URL to data/tunnel-url.txt, the same file tunnel.sh's
poller writes, then stays alive for as long as the tunnel does. Free-tier
ngrok shows its "Visit Site" warning page to browsers — click through once.
"""
from __future__ import annotations

import os
import time
from pathlib import Path


def main() -> int:
    token = os.environ.get("NGROK_AUTHTOKEN", "").strip()
    if not token:
        print("[tunnel] NGROK_AUTHTOKEN is not set; cannot start the tunnel",
              flush=True)
        return 1

    from pyngrok import ngrok, conf

    conf.get_default().auth_token = token
    port = int(os.environ.get("PORT", "8787"))
    url_file = Path("data/tunnel-url.txt")
    url_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        url_file.chmod(0o600)
    except OSError:
        pass

    ngrok.connect(port, "http")
    url = ""
    for _ in range(20):
        for t in ngrok.get_tunnels():
            if t.public_url.startswith("https://"):
                url = t.public_url
                break
        if url:
            break
        time.sleep(1)
    if not url:
        print("[tunnel] ngrok opened but no https URL appeared", flush=True)
        return 2

    url_file.write_text(url + "\n")
    print("[tunnel] up: %s" % url, flush=True)
    print("[tunnel] first visit shows ngrok's warning page — click 'Visit Site' once.", flush=True)

    # Stay alive; if the tunnel dies, exit non-zero so the log says why.
    try:
        while True:
            time.sleep(5)
            if not ngrok.get_tunnels():
                print("[tunnel] tunnel lost", flush=True)
                return 2
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
