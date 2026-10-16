"""Image preprocessing for Frame uploads.

The Frame's Art Mode renders at 3840x2160 (16:9). Source photos are rarely
that aspect, so we contain-fit and pad with black. This matches the PRD:
"pad-to-fit 3840x2160 with black background, JPEG quality ~92."
"""
from __future__ import annotations

import io
import logging

from PIL import Image, ImageOps

log = logging.getLogger(__name__)

TARGET_W, TARGET_H = 3840, 2160
JPEG_QUALITY = 92


def prepare_for_frame(raw: bytes) -> bytes:
    """Decode arbitrary bytes (JPEG/PNG/HEIC-if-Pillow-supports/etc.), contain
    to 3840x2160, pad with black, return JPEG bytes. Honors EXIF orientation."""
    src = Image.open(io.BytesIO(raw))
    src = ImageOps.exif_transpose(src)
    src = src.convert("RGB")

    canvas = Image.new("RGB", (TARGET_W, TARGET_H), (0, 0, 0))
    fitted = ImageOps.contain(src, (TARGET_W, TARGET_H), method=Image.LANCZOS)
    offset = ((TARGET_W - fitted.width) // 2, (TARGET_H - fitted.height) // 2)
    canvas.paste(fitted, offset)

    out = io.BytesIO()
    canvas.save(out, "JPEG", quality=JPEG_QUALITY, optimize=True)
    log.info(
        f"prepared image: {src.size} -> {fitted.size} on {TARGET_W}x{TARGET_H} "
        f"({len(raw)}B in, {out.tell()}B out)"
    )
    return out.getvalue()
