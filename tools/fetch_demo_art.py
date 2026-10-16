#!/usr/bin/env python3
"""Fill a demo library with public-domain paintings for DOCENT_DEMO mode.

Downloads a dozen well-known works from the Art Institute of Chicago's open
API (all CC0), crops them to 16:9 like the TV's own thumbnails, and records
their titles so the demo shows real names.

Writes to demo-data/ by default — never your real data/ folder, whose
thumbnails and content ids belong to your TV.

Usage (from the repo root, venv active):
    python tools/fetch_demo_art.py
    DOCENT_DEMO=1 DATA_DIR=demo-data ./run.sh
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import urllib.request
from pathlib import Path

os.environ.setdefault("DATA_DIR", "demo-data")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image, ImageOps  # noqa: E402

from app import db  # noqa: E402
from app.config import settings  # noqa: E402

# Art Institute of Chicago artwork ids — public domain, landscape-ish.
ARTWORKS = [
    64818,   # Monet, Stacks of Wheat (End of Summer)
    20684,   # Caillebotte, Paris Street; Rainy Day
    28560,   # van Gogh, The Bedroom
    24645,   # Hokusai, Under the Wave off Kanagawa
    90048,   # Thomas Cole, Distant View of Niagara Falls
    14620,   # Monet, Cliff Walk at Pourville
    25865,   # Winslow Homer, The Herring Net
    16571,   # Monet, Arrival of the Normandy Train, Gare Saint-Lazare
    14598,   # Monet, The Beach at Sainte-Adresse
    4796,    # Turner, Fishing Boats with Hucksters Bargaining for Fish
    897,     # Constant Troyon, Pasture in Normandy
    27992,   # Seurat, A Sunday on La Grande Jatte — last, so demo mode shows it on the TV
]
API = "https://api.artic.edu/api/v1/artworks/{id}?fields=id,title,image_id,is_public_domain"
IIIF = "https://www.artic.edu/iiif/2/{image_id}/full/843,/0/default.jpg"
_UA = "Docent demo-art fetcher (https://github.com/doaxs/docent)"
HEADERS = {"AIC-User-Agent": _UA, "User-Agent": _UA}  # IIIF 403s the default urllib agent
SIZE = (768, 432)  # 2x the TV's 384x216 thumbnails, for crisp screenshots


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def main() -> None:
    if settings.data_dir.resolve() == (Path.cwd() / "data").resolve():
        sys.exit("Refusing to write into data/ — that's your real TV's cache. Use DATA_DIR=demo-data.")
    settings.thumb_dir.mkdir(parents=True, exist_ok=True)
    db.init()

    n = 0
    for art_id in ARTWORKS:
        info = json.loads(_get(API.format(id=art_id)))["data"]
        if not (info.get("is_public_domain") and info.get("image_id")):
            print(f"  skip {art_id}: not public domain / no image")
            continue
        im = Image.open(io.BytesIO(_get(IIIF.format(image_id=info["image_id"])))).convert("RGB")
        im = ImageOps.fit(im, SIZE, Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=88)
        data = buf.getvalue()

        # "Stacks of Wheat (End of Summer)" -> "Stacks of Wheat"; drops long subtitles.
        title = info["title"].split(" (")[0].split(" — ")[0]
        n += 1
        cid = f"MY_F{n:04d}"
        (settings.thumb_dir / f"{cid}.jpg").write_bytes(data)
        db.record_upload(
            content_id=cid, filename=title, sha256=hashlib.sha256(data).hexdigest(),
            source_width=im.width, source_height=im.height, raw_bytes=len(data),
        )
        print(f"  {cid}  {title}")

    print(f"\n{n} paintings in {settings.thumb_dir}. Run:\n  DOCENT_DEMO=1 DATA_DIR={settings.data_dir} ./run.sh")


if __name__ == "__main__":
    main()
