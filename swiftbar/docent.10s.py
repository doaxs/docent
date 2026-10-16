#!/usr/bin/env python3
# <xbar.title>Docent</xbar.title>
# <xbar.desc>Art Mode status and quick controls for your Samsung Frame.</xbar.desc>
# <swiftbar.hideAbout>true</swiftbar.hideAbout>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.hideLastUpdated>true</swiftbar.hideLastUpdated>
# <swiftbar.hideDisablePlugin>true</swiftbar.hideDisablePlugin>
# <swiftbar.hideSwiftBar>true</swiftbar.hideSwiftBar>
"""SwiftBar plugin for Docent.

Filename convention: <name>.<interval>.<ext> — SwiftBar reruns every <interval>
to refresh the menu. 10s matches the dashboard's own status poll (each tick
hits the dashboard, which hits the TV).

Talks to whichever Docent server is up: the Mac app (port 8765) first, then
the dev server from ./run.sh (port 8000). Set DOCENT_URL to override.

Output protocol: print menu lines in SwiftBar format. The first line before
the `---` separator is the menu bar title.
See https://github.com/swiftbar/SwiftBar#plugin-api
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

APP_URL = "http://127.0.0.1:8765"   # Docent.app (desktop.py)
DEV_URL = "http://127.0.0.1:8000"   # ./run.sh
BUNDLE_ID = "io.github.doaxs.docent"
# Generous: /api/status waits on the TV, which can take up to its 8s timeout
# when asleep. A connection refused (server down) still fails instantly.
TIMEOUT = 10
SELF = sys.argv[0]
RUN_SH = Path(__file__).resolve().parent.parent / "run.sh"
THUMB_CACHE = Path(tempfile.gettempdir()) / "docent-swiftbar"

# Same tokens as style.css — light,dark pairs.
GREEN = "#28A745,#32D74B"
ORANGE = "#C93400,#FF9F0A"
RED = "#E5352B,#FF6961"
GRAY = "#8E8E93,#98989D"


def fetch_status() -> tuple[str, dict] | None:
    """(base_url, status) from the first Docent server that answers."""
    urls = [os.environ["DOCENT_URL"]] if os.environ.get("DOCENT_URL") else [APP_URL, DEV_URL]
    for base in urls:
        try:
            with urllib.request.urlopen(f"{base}/api/status", timeout=TIMEOUT) as r:
                return base, json.loads(r.read().decode("utf-8", errors="replace"))
        except (urllib.error.URLError, OSError, ValueError):
            continue
    return None


def post(base: str, path: str, data: bytes = b"") -> None:
    req = urllib.request.Request(f"{base}{path}", data=data, method="POST")
    try:
        urllib.request.urlopen(req, timeout=TIMEOUT).close()
    except (urllib.error.URLError, OSError):
        pass


def thumb_b64(base: str, cid: str) -> str | None:
    """Current artwork as base64 PNG, tagged 144 dpi so the menu draws the
    384×216 thumbnail at 192×108 pt — crisp on Retina. Cached per content id."""
    cached = THUMB_CACHE / f"{cid}.b64"
    if cached.exists():
        return cached.read_text()
    try:
        with urllib.request.urlopen(f"{base}/thumb/{cid}", timeout=TIMEOUT) as r:
            data = r.read()
        THUMB_CACHE.mkdir(exist_ok=True)
        jpg, png = THUMB_CACHE / f"{cid}.jpg", THUMB_CACHE / f"{cid}.png"
        jpg.write_bytes(data)
        # sips won't retag a JPEG's dpi, so convert to PNG on the way.
        subprocess.run(
            ["sips", "-s", "format", "png", "-s", "dpiWidth", "144", "-s", "dpiHeight", "144",
             str(jpg), "--out", str(png)],
            capture_output=True, timeout=5, check=True,
        )
        encoded = base64.b64encode(png.read_bytes()).decode("ascii")
        jpg.unlink()
        png.unlink()
        cached.write_text(encoded)
        return encoded
    except (urllib.error.URLError, OSError, subprocess.SubprocessError):
        return None


def action(label: str, cmd: str, base: str, extra: str = "") -> str:
    return f"{label} | bash='{SELF}' param1={cmd} param2={base} terminal=false refresh=true {extra}".rstrip()


def open_line(base: str) -> str:
    # The Mac app's server only runs inside the app — bring the window up.
    # The dev server has no window, so open it in the browser.
    if base == APP_URL:
        return f"Open Docent | bash=/usr/bin/open param1=-b param2={BUNDLE_ID} terminal=false sfimage=macwindow"
    return f"Open Docent in Browser | href={base} sfimage=safari"


def run_action(cmd: str, base: str) -> None:
    if cmd == "artmode-on":
        post(base, "/artmode", b"on=1")
    elif cmd == "artmode-off":
        post(base, "/artmode", b"on=0")
    elif cmd == "slideshow-toggle":
        post(base, "/slideshow/toggle")
    elif cmd == "wake":
        post(base, "/wake")


def main() -> None:
    # SwiftBar invokes the script with no args to render, and with
    # param1/param2 (configured on the menu lines below) for actions.
    if len(sys.argv) > 2:
        run_action(sys.argv[1], sys.argv[2])
        return

    found = fetch_status()

    if found is None:
        print(f":photo.artframe: ∘ | color={GRAY} symbolize=true size=16")
        print("---")
        print(f"Docent isn’t running | sfimage=moon.zzz sfcolor={GRAY}")
        print("---")
        print(f"Open Docent | bash=/usr/bin/open param1=-b param2={BUNDLE_ID} terminal=false refresh=true sfimage=macwindow")
        if RUN_SH.exists():
            print(f"Start Dev Server in Terminal | bash=/bin/bash param1=-lc param2='{RUN_SH}' terminal=true sfimage=terminal")
        return

    base, s = found
    state = s.get("state")
    art_on = s.get("artmode") == "on"
    ss_on = bool((s.get("slideshow") or {}).get("running"))

    dot = {"connected": GREEN, "unreachable": ORANGE, "pairing": ORANGE}.get(state, GRAY)
    # SF Symbol stays template-tinted by the menu bar; the trailing dot
    # carries the status color so the TV's state is glanceable.
    print(f":photo.artframe: • | color={dot} symbolize=true size=16")
    print("---")

    if state == "connected":
        mode = ("Slideshow" if ss_on else "Art Mode") if art_on else "Watching TV"
        print(f"On · {mode} | sfimage=circle.fill sfcolor={GREEN}")
        print(f"{s.get('ip')} | size=11 color={GRAY}")
        if s.get("showing"):
            print("---")
            img = thumb_b64(base, s["showing_id"]) if s.get("showing_id") else None
            if img:
                print(f"| image={img}")
            print(f"{s['showing']} | sfimage=photo")
        print("---")
        print(action("Art Mode", "artmode-off" if art_on else "artmode-on", base,
                     f"checked={'true' if art_on else 'false'}"))
        print(action("Slideshow", "slideshow-toggle", base, f"checked={'true' if ss_on else 'false'}"))
    elif state == "unreachable":
        print(f"Asleep | sfimage=moon.fill sfcolor={ORANGE}")
        print(f"{s.get('ip')} | size=11 color={GRAY}")
        print("---")
        print(action("Wake TV", "wake", base, "sfimage=power"))
    elif state == "pairing":
        print(f"Waiting for Allow… | sfimage=hourglass sfcolor={ORANGE}")
        print(f"Accept the prompt on your TV with the remote. | size=11 color={GRAY}")
    else:
        print(f"No TV yet | sfimage=circle sfcolor={GRAY}")
        print(f"Set up your TV in Docent. | size=11 color={GRAY}")

    print("---")
    print(open_line(base))
    print("Refresh | refresh=true sfimage=arrow.clockwise")


if __name__ == "__main__":
    main()
