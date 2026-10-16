"""Singleton wrapper around samsungtvws.

Each FastAPI worker keeps one instance. On any failure the connection is
reset so the next call attempts a fresh connect — this avoids dead-socket
issues when the TV goes to sleep and back.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable, TypeVar

from samsungtvws import SamsungTVWS

from .config import settings

log = logging.getLogger(__name__)

T = TypeVar("T")


class TVClient:
    """Serializes TV access through a single lock. The Tizen WS API is
    single-channel — concurrent calls from multiple uvicorn worker threads
    can wedge the connection. The lock keeps one call in flight at a time;
    callers that can't acquire within `_LOCK_WAIT_SECONDS` fail fast with
    the method's default value rather than tying up an HTTP worker."""

    _LOCK_WAIT_SECONDS = 4
    _AVAILABLE_IDS_TTL = 30.0

    def __init__(self) -> None:
        self._tv: SamsungTVWS | None = None
        self._art = None
        self._lock = threading.Lock()
        self._matte_colors_cache: list[dict[str, Any]] | None = None
        self._matte_types_cache: list[str] | None = None
        self._available_cache: tuple[float, list[dict[str, Any]]] | None = None
        self._last_available: list[dict[str, Any]] = []

    def _art_iface(self):
        if self._tv is None:
            log.info(f"Connecting to TV at {settings.tv_ip}")
            self._tv = SamsungTVWS(
                host=settings.tv_ip,
                token_file=str(settings.token_file),
                timeout=8,
                name="Docent",
            )
            self._art = self._tv.art()
            log.info("TV connection established")
        return self._art

    def _reset(self) -> None:
        self._tv = None
        self._art = None

    def reconnect(self) -> None:
        """Force the next call to open a fresh connection. Use after the TV
        IP changes — the stored token is per-TV, not per-IP, so it should
        still be valid; if not, callers will see a clean connect failure
        and can delete the token file."""
        with self._lock:
            self._reset()

    def _call(self, op: str, default: T, fn: Callable[[Any], T]) -> T:
        """Run `fn(art_iface)` under the lock with logging + error handling.
        Returns `default` if another call is in flight or anything fails."""
        if not self._lock.acquire(timeout=self._LOCK_WAIT_SECONDS):
            log.warning(f"{op}: another TV call is in flight, skipping")
            return default
        try:
            log.info(f"TV call: {op}")
            return fn(self._art_iface())
        except Exception as e:
            log.warning(f"{op} failed: {e}")
            self._reset()
            return default
        finally:
            self._lock.release()

    # ---- read-only API ----------------------------------------------------

    def supported(self) -> bool:
        return self._call("supported()", False, lambda a: bool(a.supported()))

    def available(self) -> list[dict[str, Any]]:
        def _go(a):
            items = a.available() or []
            # Normalize string entries (older firmware) to dicts, and dedupe
            # by content_id (2026 firmware returns same id once per category).
            seen: set[str] = set()
            out: list[dict[str, Any]] = []
            for it in items:
                norm = it if isinstance(it, dict) else {"content_id": str(it)}
                cid = str(norm.get("content_id", ""))
                if not cid or cid in seen:
                    continue
                seen.add(cid)
                out.append(norm)
            return out

        items = self._call("available()", [], _go)
        if items:
            self._available_cache = (time.time(), items)
            self._last_available = items
        return items

    def last_known(self) -> list[dict[str, Any]]:
        """The last library the TV reported (this process lifetime). Lets the
        UI keep showing art, greyed out, while the TV is asleep."""
        return self._last_available

    def current(self) -> dict[str, Any] | None:
        """Returns the TV's reported current image, or None.

        The 2026 firmware sometimes returns a stale content_id pointing at a
        deleted image — its `get_current()` pointer isn't always synced with
        the library. We validate the id against a short-cached `available()`
        snapshot and drop the field if it's a ghost (the TV is showing
        something but the API is lying about which one)."""
        def _go(a):
            r = a.get_current()
            return r if isinstance(r, dict) else None
        result = self._call("get_current()", None, _go)
        if not result:
            return None
        cid = result.get("content_id")
        if cid and cid not in self._cached_available_ids():
            log.info(f"current() returned ghost id {cid} (not in library); dropping")
            return None
        return result

    def available_cached(self) -> list[dict[str, Any]]:
        """`available()` snapshot cached for _AVAILABLE_IDS_TTL seconds. Used
        by the 10s status poll (ghost-id check, current image's matte) so it
        doesn't hit the TV for the full library every tick."""
        if self._available_cache and time.time() - self._available_cache[0] < self._AVAILABLE_IDS_TTL:
            return self._available_cache[1]
        # available() refreshes the cache on success; an empty result (could
        # be a transient lock miss) is never cached.
        return self.available()

    def _cached_available_ids(self) -> set[str]:
        """Content_ids known to the TV, from the cached snapshot. Empty on
        lookup failure (caller will treat as 'don't filter')."""
        ids = {it.get("content_id", "") for it in self.available_cached() if isinstance(it, dict)}
        ids.discard("")
        return ids

    def invalidate_available_cache(self) -> None:
        """Call after upload/delete/matte change so the next lookup sees the
        post-mutation library."""
        self._available_cache = None

    def artmode(self) -> str | None:
        return self._call("get_artmode()", None, lambda a: a.get_artmode())

    def thumbnail(self, content_id: str) -> bytes | None:
        """Fetch JPEG thumbnail bytes. Uses the batch endpoint because the
        single-fetch get_thumbnail() hangs on 2026 firmware."""
        def _go(a):
            result = a.get_thumbnail_list([content_id]) or {}
            for v in result.values():
                if v:
                    return bytes(v)
            return None

        return self._call(f"get_thumbnail_list([{content_id}])", None, _go)

    # ---- write API --------------------------------------------------------

    def set_artmode(self, on: bool) -> bool:
        def _go(a):
            a.set_artmode(on)
            return True
        return self._call(f"set_artmode({'on' if on else 'off'})", False, _go)

    def cache_mac(self) -> str | None:
        """Best-effort: fetch the TV's MAC via REST and persist to disk so
        wake() can use it when the TV is fully off. Silent on any failure."""
        try:
            if self._tv is None:
                self._art_iface()  # ensure connection
            info = self._tv.rest_device_info() or {}
            mac = (info.get("device") or {}).get("wifiMac")
            if mac and len(mac) >= 11:
                settings.mac_file.write_text(mac.strip())
                return mac
        except Exception as e:
            log.debug(f"cache_mac failed: {e}")
        return None

    def wake(self) -> bool:
        """Send a Wake-on-LAN magic packet to the TV's stored MAC.
        Returns False if no MAC is cached or the broadcast fails. The TV
        must have 'Network Standby' enabled in its settings for WoL to work."""
        if not settings.mac_file.exists():
            log.warning("wake(): no cached MAC; bring TV online once first")
            return False
        mac = settings.mac_file.read_text().strip()
        try:
            mac_bytes = bytes.fromhex(mac.replace(":", "").replace("-", ""))
        except ValueError:
            log.warning(f"wake(): invalid MAC {mac!r}")
            return False
        if len(mac_bytes) != 6:
            return False
        packet = b"\xff" * 6 + mac_bytes * 16
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            for port in (9, 7):
                s.sendto(packet, ("255.255.255.255", port))
            s.close()
            log.info(f"wake(): WoL magic packet sent to {mac}")
            return True
        except Exception as e:
            log.warning(f"wake() send failed: {e}")
            return False

    def power_off(self) -> bool:
        """Send KEY_POWER to fully power off the TV. Uses a dedicated
        remote-channel SamsungTVWS instance with its own token file —
        the art and remote channels are authorized separately by the TV."""
        def _go(_a):
            remote = SamsungTVWS(
                host=settings.tv_ip,
                port=8002,  # encrypted channel — required for remote keys on 2020+ Tizen
                token_file=str(settings.remote_token_file),
                timeout=8,
                name="Docent",
            )
            try:
                remote.shortcuts().power()
                return True
            finally:
                try:
                    remote.close()
                except Exception:
                    pass
        return self._call("power_off()", False, _go)

    def select_image(self, content_id: str) -> bool:
        def _go(a):
            a.select_image(content_id, show=True)
            return True
        return self._call(f"select_image({content_id})", False, _go)

    def _ensure_matte_lists(self) -> None:
        """Populate matte_types and matte_colors caches in one TV round-trip."""
        if self._matte_colors_cache is not None and self._matte_types_cache is not None:
            return
        def _go(a):
            return a.get_matte_list() or {}
        ml = self._call("get_matte_list()", {}, _go)
        if ml:
            self._matte_colors_cache = ml.get("matte_colors") or []
            self._matte_types_cache = [
                m["matte_type"] for m in (ml.get("matte_types") or []) if isinstance(m, dict)
            ]

    def matte_colors(self) -> list[dict[str, Any]]:
        """Cached list of available matte colors:
        [{'color': 'black', 'R': 34, 'G': 34, 'B': 33}, ...]."""
        self._ensure_matte_lists()
        return self._matte_colors_cache or []

    def matte_types(self) -> list[str]:
        """Cached list of matte type names, e.g. ['none', 'modern',
        'flexible', 'shadowbox', ...]. The TV may reject some combinations
        if the image's aspect ratio is incompatible — the user gets a
        toast and can pick another."""
        self._ensure_matte_lists()
        return self._matte_types_cache or []

    def change_matte(self, content_id: str, matte_id: str) -> bool:
        """Set the matte for an image. matte_id is `none` or `<type>_<color>`."""
        def _go(a):
            r = a.change_matte(content_id, matte_id=matte_id)
            return isinstance(r, dict) and r.get("matte_id") == matte_id
        return self._call(f"change_matte({content_id}, {matte_id})", False, _go)

    def delete(self, content_id: str) -> bool:
        """Delete a single image from the TV's library."""
        def _go(a):
            return bool(a.delete_list([content_id]))
        return self._call(f"delete_list([{content_id}])", False, _go)

    def upload(self, jpeg_bytes: bytes, *, matte: str = "flexible_polar") -> str | None:
        """Push pre-processed JPEG bytes (3840x2160) to the TV.
        Returns the new content_id, or None on failure.
        Default matte is flexible_polar — non-flexible mattes 502 on this firmware."""
        def _go(a):
            cid = a.upload(jpeg_bytes, file_type="JPEG", matte=matte, portrait_matte=matte)
            return cid if isinstance(cid, str) and cid else None

        return self._call(
            f"upload(JPEG, {len(jpeg_bytes)}B, matte={matte})",
            None,
            _go,
        )


if os.environ.get("DOCENT_DEMO"):
    # Demo mode: a fake in-memory TV built from cached thumbnails. Lets you
    # work on the UI (or take screenshots) without touching the real TV.
    from .demo import DemoTV
    tv = DemoTV()
else:
    tv = TVClient()
