"""App-side slideshow.

The 2026 Frame Pro firmware rejects samsungtvws's `set_slideshow_status` with
error -7 for every parameter shape we've tried, and `set_auto_rotation_status`
times out (the TV doesn't recognize the request). Rather than wait for either
the firmware or the library to be fixed, we implement slideshow in-process:
a background thread that calls `tv.select_image(next_cid)` every N minutes.
The TV is just being told to display a specific image — that path works fine.
"""
from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class SlideshowStatus:
    running: bool
    interval_min: int
    shuffle: bool
    last_change_at: float | None
    last_cid: str | None


class Slideshow:
    """Single-instance app-side slideshow controller. The thread sleeps
    between changes; `stop()` interrupts the sleep so reconfiguring is
    quick. Re-entrant calls to `start()` cleanly stop the previous run
    before launching a new one."""

    def __init__(self, tv) -> None:
        self._tv = tv
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_evt = threading.Event()
        self._interval_min = 0
        self._shuffle = True
        self._last_change_at: float | None = None
        self._last_cid: str | None = None

    def status(self) -> SlideshowStatus:
        return SlideshowStatus(
            running=self._thread is not None and self._thread.is_alive(),
            interval_min=self._interval_min,
            shuffle=self._shuffle,
            last_change_at=self._last_change_at,
            last_cid=self._last_cid,
        )

    def start(self, interval_min: int, shuffle: bool) -> None:
        with self._lock:
            self._stop_locked()
            self._interval_min = max(1, int(interval_min))
            self._shuffle = bool(shuffle)
            self._stop_evt = threading.Event()
            self._thread = threading.Thread(
                target=self._run,
                name=f"slideshow({self._interval_min}m,{'shuf' if self._shuffle else 'order'})",
                daemon=True,
            )
            self._thread.start()
            log.info(
                f"slideshow started: every {self._interval_min} min, "
                f"{'shuffled' if self._shuffle else 'in order'}"
            )

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        """Caller must hold self._lock."""
        if self._thread is not None and self._thread.is_alive():
            self._stop_evt.set()
            # Don't join under our lock — the thread itself doesn't take it,
            # but joining holds up the caller. A short join is fine here
            # because the thread checks the event between TV calls.
        self._thread = None
        log.info("slideshow stopped")

    def _pick_next(self, items: list[dict], current_cid: str | None) -> str | None:
        """Pick the next content_id to display.
        Personal images (MY_*) only — Samsung Store items shouldn't be
        rotated through automatically (they belong to the user's curated
        library, not their photos).
        """
        candidates = [
            it["content_id"] for it in items
            if isinstance(it, dict) and str(it.get("content_id", "")).startswith("MY")
        ]
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]

        if self._shuffle:
            # Avoid repeating the current image immediately
            choices = [c for c in candidates if c != current_cid] or candidates
            return random.choice(choices)

        # In-order rotation
        if current_cid in candidates:
            i = candidates.index(current_cid)
            return candidates[(i + 1) % len(candidates)]
        return candidates[0]

    def _run(self) -> None:
        # Tick once immediately on start so the user sees something change
        # within seconds rather than waiting a full interval.
        first_tick = True
        while not self._stop_evt.is_set():
            if not first_tick:
                # Sleep in small chunks so stop() takes effect quickly.
                deadline = time.monotonic() + self._interval_min * 60
                while time.monotonic() < deadline:
                    if self._stop_evt.wait(timeout=min(2.0, deadline - time.monotonic())):
                        return
            first_tick = False

            try:
                items = self._tv.available()
                cur = self._tv.current() or {}
                next_cid = self._pick_next(items, cur.get("content_id"))
                if next_cid is None:
                    log.warning("slideshow tick: no MY_* candidates, sleeping until next tick")
                    continue
                if self._tv.select_image(next_cid):
                    self._last_change_at = time.time()
                    self._last_cid = next_cid
                    log.info(f"slideshow: switched to {next_cid}")
                else:
                    log.warning(f"slideshow: select_image({next_cid}) returned False")
            except Exception as e:
                log.warning(f"slideshow tick failed: {e}")
