"""Fake in-memory TV for UI work and screenshots — enabled with DOCENT_DEMO=1.

Mirrors the public surface of `tv.TVClient` so routes don't know the
difference. The library is built from whatever is in `data/thumbs/`, so
copying a real thumbnail cache in gives you a realistic dashboard without
any network traffic to the TV.

    DOCENT_DEMO=1 uvicorn app.main:app --port 8000

DOCENT_DEMO_STATE=unreachable|pairing (optional) pins the connection state.
"""
from __future__ import annotations

import io
import logging
import os
from typing import Any

from PIL import Image

from .config import settings

log = logging.getLogger(__name__)

_COLORS = [
    ("polar", 242, 242, 238), ("neutral", 218, 214, 206), ("warm", 234, 219, 196),
    ("antique", 223, 207, 174), ("sand", 201, 178, 140), ("apricot", 237, 176, 136),
    ("redorange", 207, 85, 48), ("burgandy", 106, 33, 49), ("byzantine", 119, 67, 111),
    ("lavender", 187, 176, 212), ("skyblue", 156, 194, 222), ("turquoise", 46, 151, 153),
    ("seafoam", 165, 200, 186), ("sage", 161, 174, 144), ("navy", 30, 43, 69),
    ("black", 26, 26, 28),
]
_TYPES = ["none", "modernthin", "modern", "modernwide", "flexible",
          "shadowbox", "panoramic", "triptych", "mix", "squares"]
# A few varied mattes so the demo library doesn't look uniform.
_SEED_MATTES = ["modern_warm", "none", "shadowbox_antique", "modernthin_polar",
                "flexible_navy", "modern_black", "modernwide_skyblue", "flexible_polar"]


class DemoTV:
    def __init__(self) -> None:
        self.state = os.environ.get("DOCENT_DEMO_STATE", "connected")
        ids = sorted(p.stem for p in settings.thumb_dir.glob("*.jpg"))
        self._items: list[dict[str, Any]] = [
            {"content_id": cid, "matte_id": _SEED_MATTES[i % len(_SEED_MATTES)]}
            for i, cid in enumerate(ids)
        ]
        self._current = next((it["content_id"] for it in reversed(self._items) if it["content_id"].startswith("MY")), None)
        self._artmode = "on"
        log.info(f"DemoTV: {len(self._items)} images from {settings.thumb_dir}")

    def _up(self) -> bool:
        return self.state == "connected"

    # ---- read-only API ----
    def supported(self) -> bool:
        return self._up()

    def available(self) -> list[dict[str, Any]]:
        return [dict(it) for it in self._items] if self._up() else []

    available_cached = available

    def last_known(self) -> list[dict[str, Any]]:
        return [dict(it) for it in self._items]

    def current(self) -> dict[str, Any] | None:
        if not self._up() or not self._current:
            return None
        it = next((i for i in self._items if i["content_id"] == self._current), None)
        return dict(it) if it else None

    def artmode(self) -> str | None:
        return self._artmode if self._up() else None

    def thumbnail(self, content_id: str) -> bytes | None:
        return None  # /thumb serves from data/thumbs/ before ever asking

    def matte_colors(self) -> list[dict[str, Any]]:
        return [{"color": c, "R": r, "G": g, "B": b} for c, r, g, b in _COLORS]

    def matte_types(self) -> list[str]:
        return list(_TYPES)

    def invalidate_available_cache(self) -> None:
        pass

    def reconnect(self) -> None:
        pass

    def cache_mac(self) -> str | None:
        return None

    # ---- write API ----
    def set_artmode(self, on: bool) -> bool:
        self._artmode = "on" if on else "off"
        return self._up()

    def wake(self) -> bool:
        self.state = "connected"
        self._artmode = "on"
        return True

    def power_off(self) -> bool:
        self.state = "unreachable"
        return True

    def select_image(self, content_id: str) -> bool:
        self._current = content_id
        self._artmode = "on"
        return self._up()

    def change_matte(self, content_id: str, matte_id: str) -> bool:
        # Mimic the firmware rejecting multi-panel mattes on some images.
        if matte_id.split("_")[0] in ("triptych", "mix") and content_id.endswith(("1", "6")):
            return False
        for it in self._items:
            if it["content_id"] == content_id:
                it["matte_id"] = matte_id
                return True
        return False

    def delete(self, content_id: str) -> bool:
        self._items = [it for it in self._items if it["content_id"] != content_id]
        if self._current == content_id:
            self._current = self._items[0]["content_id"] if self._items else None
        return True

    def upload(self, jpeg_bytes: bytes, *, matte: str = "flexible_polar") -> str | None:
        n = max([int(it["content_id"][4:]) for it in self._items if it["content_id"].startswith("MY_F")] or [0]) + 1
        cid = f"MY_F{n:04d}"
        with Image.open(io.BytesIO(jpeg_bytes)) as im:
            im.convert("RGB").resize((384, 216)).save(settings.thumb_dir / f"{cid}.jpg", quality=88)
        self._items.append({"content_id": cid, "matte_id": matte})
        return cid
