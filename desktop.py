"""Desktop launcher: runs uvicorn in a background thread and opens a
native macOS WebKit window pointed at the local server.

Used both for `python desktop.py` during development of the bundled flow,
and as the entry point for the PyInstaller-built .app.
"""
import os
import socket
import sys
import threading
import time
from pathlib import Path


def _user_data_dir() -> Path:
    """Stable, writable per-user location for .env, token, thumbs, sqlite.
    Used as cwd so the app's relative paths (./data, ./.env) resolve
    correctly whether run from source or from a bundled .app."""
    support = Path.home() / "Library" / "Application Support"
    base = support / "Docent"
    legacy = support / "FrameDashboard"  # pre-rename location — keep the pairing token
    if legacy.is_dir() and not base.exists():
        legacy.rename(base)
    base.mkdir(parents=True, exist_ok=True)
    (base / "data").mkdir(exist_ok=True)
    return base


def _wait_for_port(host: str, port: int, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def _run_server(host: str, port: int) -> None:
    import uvicorn
    from app.main import app  # imported here so cwd is already set

    uvicorn.run(app, host=host, port=port, log_level="info")


def main() -> None:
    os.chdir(_user_data_dir())

    # Ensure bundled app can find the `app` package when frozen.
    if getattr(sys, "frozen", False):
        sys.path.insert(0, str(Path(sys._MEIPASS)))  # type: ignore[attr-defined]

    host, port = "127.0.0.1", 8765
    threading.Thread(target=_run_server, args=(host, port), daemon=True).start()

    if not _wait_for_port(host, port):
        print("Server failed to start on", port, file=sys.stderr)
        sys.exit(1)

    open_window(f"http://{host}:{port}/")


def _unify_titlebar(window) -> None:
    """macOS 27-style window: full-height translucent content under a
    transparent unified titlebar, traffic lights floating over the page's
    own toolbar. Must run on the main thread after the window exists."""
    from AppKit import (
        NSColor,
        NSToolbar,
        NSWindowStyleMaskFullSizeContentView,
        NSWindowTitleHidden,
        NSWindowToolbarStyleUnified,
    )

    ns = window.native
    ns.setHasShadow_(True)  # pywebview drops the shadow for transparent windows
    ns.setTitlebarAppearsTransparent_(True)
    ns.setTitleVisibility_(NSWindowTitleHidden)
    ns.setStyleMask_(ns.styleMask() | NSWindowStyleMaskFullSizeContentView)
    # pywebview paints the titlebar with an opaque window color; clear it.
    ns.contentView().superview().subviews().lastObject().setBackgroundColor_(NSColor.clearColor())
    # An empty unified toolbar makes the titlebar 44pt tall so the traffic
    # lights line up with the page's toolbar instead of hugging the top edge.
    toolbar = NSToolbar.alloc().initWithIdentifier_("docent")
    toolbar.setShowsBaselineSeparator_(False)
    ns.setToolbar_(toolbar)
    ns.setToolbarStyle_(NSWindowToolbarStyleUnified)


def open_window(url: str) -> None:
    import webview
    from PyObjCTools import AppHelper

    # The page marks its toolbar background with .pywebview-drag-region;
    # direct-target-only keeps buttons inside it clickable.
    webview.settings["DRAG_REGION_DIRECT_TARGET_ONLY"] = True
    window = webview.create_window(
        "Docent",
        f"{url}?window=vibrant",
        width=1280,
        height=860,
        min_size=(900, 600),
        transparent=True,
        vibrancy=True,
    )
    window.events.shown += lambda: AppHelper.callAfter(_unify_titlebar, window)
    webview.start()


if __name__ == "__main__":
    main()
