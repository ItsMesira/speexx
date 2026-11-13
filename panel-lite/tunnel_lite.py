#!/usr/bin/env python3
"""tunnel_lite.py — public URL for the lite panel through pyngrok.

Same deal as panel-cloud's tunnel_ngrok.py: needs pyngrok in the chosen
python and NGROK_AUTHTOKEN in the environment. Writes the URL to
lite_data/tunnel-url.txt and stays alive for as long as the tunnel does.
"""
from __future__ import annotations

import os
import time
from pathlib import Path


def main() -> int:
    token = os.environ.get("NGROK_AUTHTOKEN", "").strip()
    if not token:
        print("[lite-tunnel] NGROK_AUTHTOKEN is not set", flush=True)
        return 1
    from pyngrok import ngrok, conf

    conf.get_default().auth_token = token
    port = int(os.environ.get("PORT", "8788"))
    url_file = Path("lite_data/tunnel-url.txt")
    url_file.parent.mkdir(parents=True, exist_ok=True)

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
        print("[lite-tunnel] opened but no https URL appeared", flush=True)
        return 2

    url_file.write_text(url + "\n")
    print("[lite-tunnel] up: %s" % url, flush=True)
    print("[lite-tunnel] first visit shows ngrok's warning page — click through once.", flush=True)
    try:
        while True:
            time.sleep(5)
            if not ngrok.get_tunnels():
                print("[lite-tunnel] tunnel lost", flush=True)
                return 2
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
