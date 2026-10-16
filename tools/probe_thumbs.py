"""Probe which thumbnail API path (if any) the 2026 Frame Pro firmware speaks.

Tries each known call path with a hard 15s wall-clock guard (SIGALRM), since
the samsungtvws `timeout` kwarg is not honored on the D2D socket code path.
Logs which calls return real bytes vs. hang vs. raise.

Usage:
    source venv/bin/activate
    python tools/probe_thumbs.py [--ip 192.168.1.20] [--cid MY_F0013]

--ip defaults to TV_IP from the environment or .env.
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from pathlib import Path

from samsungtvws import SamsungTVWS

TOKEN_FILE = Path(__file__).resolve().parent.parent / "data" / "tv_token.txt"
GUARD_SECONDS = 15


def _default_ip() -> str | None:
    """TV_IP from the environment, else from the repo's .env."""
    if os.environ.get("TV_IP"):
        return os.environ["TV_IP"]
    env = TOKEN_FILE.parent.parent / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.strip().startswith("TV_IP="):
                return line.split("=", 1)[1].strip() or None
    return None


class HardTimeout(Exception):
    pass


def _alarm(_sig, _frame):
    raise HardTimeout(f"hard wall-clock timeout after {GUARD_SECONDS}s")


def run(label: str, fn):
    print(f"\n--- {label} ---", flush=True)
    signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(GUARD_SECONDS)
    t0 = time.time()
    try:
        result = fn()
        elapsed = time.time() - t0
        if result is None:
            print(f"  result: None  ({elapsed:.2f}s)")
        elif isinstance(result, (bytes, bytearray)):
            print(f"  result: {len(result)} bytes  ({elapsed:.2f}s)  OK")
        elif isinstance(result, dict):
            sizes = {k: len(v) for k, v in result.items()}
            print(f"  result: dict {sizes}  ({elapsed:.2f}s)  OK")
        elif isinstance(result, list):
            print(f"  result: list, {len(result)} items, sizes={[len(x) for x in result]}  ({elapsed:.2f}s)")
        else:
            print(f"  result: {type(result).__name__}: {result!r:.120}  ({elapsed:.2f}s)")
    except HardTimeout as e:
        print(f"  HUNG: {e}")
    except Exception as e:
        elapsed = time.time() - t0
        print(f"  RAISED ({elapsed:.2f}s): {type(e).__name__}: {e}")
    finally:
        signal.alarm(0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ip", default=_default_ip(), required=_default_ip() is None)
    ap.add_argument("--cid", default=None, help="content_id to fetch (defaults to first MY_* on TV)")
    args = ap.parse_args()

    tv = SamsungTVWS(host=args.ip, token_file=str(TOKEN_FILE), timeout=10)
    art = tv.art()

    print(f"Connecting to {args.ip} (token={TOKEN_FILE})", flush=True)

    cid = args.cid
    if cid is None:
        signal.signal(signal.SIGALRM, _alarm)
        signal.alarm(GUARD_SECONDS)
        try:
            items = art.available() or []
        finally:
            signal.alarm(0)
        my = [it for it in items if isinstance(it, dict) and str(it.get("content_id", "")).startswith("MY_")]
        if not my:
            print("No MY_* items found on TV — pass --cid explicitly")
            sys.exit(1)
        cid = my[0]["content_id"]
    print(f"Using content_id: {cid}")

    run("get_thumbnail(cid)", lambda: art.get_thumbnail(cid))
    run("get_thumbnail(cid, as_dict=True)", lambda: art.get_thumbnail(cid, as_dict=True))
    run("get_thumbnail_list([cid])", lambda: art.get_thumbnail_list([cid]))

    print("\nDone.")


if __name__ == "__main__":
    main()
