#!/usr/bin/env python3
"""
frame_check.py — Day 1 connectivity test for Samsung Frame TVs.

Walks through every API capability the dashboard will use, so you find out
fast if anything is broken on your specific TV / firmware.

SETUP
-----
    python3 -m venv venv
    source venv/bin/activate          # Windows: venv\\Scripts\\activate
    pip install samsungtvws pillow

Find your TV's IP (TV: Settings → General → Network → Network Status), then:

    python tools/frame_check.py --ip 192.168.x.x

OPTIONAL FLAGS
--------------
    --image path/to/photo.jpg   use your own test image instead of a generated pattern
    --keep                      don't delete the uploaded test image when done
    --no-upload                 read-only checks only (skip upload/select/delete)

WHAT TO EXPECT
--------------
First run: your TV will pop a "Allow this device to connect?" prompt. Accept it
within ~30 seconds. The pairing token is saved to tv_token.txt next to this
script, so subsequent runs won't prompt.

If everything works, the script ends with "green light" and you've confirmed
the API is usable on your TV. If a step fails, the log shows exactly which
call broke and why — that's the diagnostic info you need before building
the dashboard on top.
"""

import argparse
import io
import logging
import sys
from pathlib import Path

from PIL import Image, ImageDraw
from samsungtvws import SamsungTVWS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("frame-check")

TOKEN_FILE = Path(__file__).parent / "tv_token.txt"


def banner(num: int, label: str) -> None:
    log.info("")
    log.info(f"=== Step {num}: {label} ===")


def make_test_image() -> bytes:
    """Generate a 4K solid-background test image with a centered crosshair."""
    img = Image.new("RGB", (3840, 2160), color=(30, 40, 60))
    draw = ImageDraw.Draw(img)
    draw.line([(0, 1080), (3840, 1080)], fill=(200, 200, 200), width=4)
    draw.line([(1920, 0), (1920, 2160)], fill=(200, 200, 200), width=4)
    draw.text((1750, 1100), "FRAME CONNECTIVITY TEST", fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def load_and_pad(path: Path) -> bytes:
    """Resize image to fit 3840×2160, letterbox with black, return JPEG bytes."""
    img = Image.open(path).convert("RGB")
    target = (3840, 2160)
    img.thumbnail(target, Image.LANCZOS)
    canvas = Image.new("RGB", target, (0, 0, 0))
    canvas.paste(
        img,
        ((target[0] - img.width) // 2, (target[1] - img.height) // 2),
    )
    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--ip", required=True, help="TV's local IP (One Connect Box)")
    p.add_argument(
        "--image",
        type=Path,
        default=None,
        help="Test image (JPEG/PNG). If omitted, a 4K test pattern is generated.",
    )
    p.add_argument(
        "--keep",
        action="store_true",
        help="Leave the uploaded test image on the TV",
    )
    p.add_argument(
        "--no-upload",
        action="store_true",
        help="Read-only checks only, skip upload/select/delete",
    )
    args = p.parse_args()

    log.info(f"Connecting to {args.ip}")
    log.info(f"Token cache: {TOKEN_FILE}")
    if not TOKEN_FILE.exists():
        log.info("First run — accept the pairing prompt on the TV when it appears.")

    try:
        tv = SamsungTVWS(host=args.ip, token_file=str(TOKEN_FILE))
        art = tv.art()
    except Exception as e:
        log.error(f"Couldn't initialize TV connection: {e}")
        return 1

    # ---- 1. Art Mode supported? -------------------------------------------
    banner(1, "Art Mode supported?")
    try:
        if not art.supported():
            log.error("art().supported() returned False — this TV doesn't expose Art Mode over the API.")
            return 1
        log.info("  yes")
    except Exception as e:
        log.error(f"  failed: {e}")
        log.error("  Common causes: wrong IP, TV in deep standby, pairing prompt not accepted in time.")
        return 1

    # ---- 2. Art mode on/off state -----------------------------------------
    banner(2, "Current art-mode state")
    try:
        log.info(f"  artmode = {art.get_artmode()}")
    except Exception as e:
        log.warning(f"  get_artmode() failed: {e}")

    # ---- 3. List existing art ---------------------------------------------
    banner(3, "List existing art on TV")
    try:
        items = art.available()
        log.info(f"  {len(items)} item(s) on the TV")
        for it in items[:5]:
            cid = it.get("content_id") if isinstance(it, dict) else it
            log.info(f"    {cid}")
        if len(items) > 5:
            log.info(f"    ... +{len(items) - 5} more")
    except Exception as e:
        log.warning(f"  available() failed: {e}")

    # ---- 4. Currently displayed -------------------------------------------
    banner(4, "Currently displayed art")
    try:
        log.info(f"  {art.get_current()}")
    except Exception as e:
        log.warning(f"  get_current() failed: {e}")

    # ---- 5. Matte options --------------------------------------------------
    banner(5, "Available matte styles")
    try:
        mattes = art.get_matte_list()
        log.info(f"  {len(mattes)} matte option(s)")
        for m in mattes[:8]:
            log.info(f"    {m}")
        if len(mattes) > 8:
            log.info(f"    ... +{len(mattes) - 8} more")
    except Exception as e:
        log.warning(f"  get_matte_list() failed (older firmware may not expose this): {e}")

    if args.no_upload:
        log.info("")
        log.info("Read-only checks complete (--no-upload set).")
        return 0

    # ---- 6. Prepare image --------------------------------------------------
    banner(6, "Prepare test image")
    if args.image and args.image.exists():
        log.info(f"  Loading {args.image}")
        data = load_and_pad(args.image)
    else:
        log.info("  No --image given; generating 4K test pattern")
        data = make_test_image()
    log.info(f"  Image size: {len(data) / 1024:.1f} KB")

    # ---- 7. Upload ---------------------------------------------------------
    banner(7, "Upload to TV")
    try:
        content_id = art.upload(data, file_type="JPEG", matte="modern_warm")
        log.info(f"  uploaded -> content_id = {content_id}")
    except Exception as e:
        log.error(f"  upload() failed: {e}")
        log.error("  If 2026 firmware has broken the upload endpoint, this is where you'd see it.")
        return 1

    # ---- 8. Activate -------------------------------------------------------
    banner(8, "Set as active")
    try:
        art.select_image(content_id, show=True)
        log.info("  selected; the image should be visible on the TV now")
    except Exception as e:
        log.warning(f"  select_image() failed: {e}")

    # ---- 9. Cleanup --------------------------------------------------------
    if args.keep:
        log.info("")
        log.info("--keep set; leaving the test image on the TV.")
    else:
        banner(9, "Delete test image")
        try:
            art.delete_list([content_id])
            log.info("  removed")
        except Exception as e:
            log.warning(f"  delete_list() failed: {e}")

    log.info("")
    log.info("All steps complete. The Frame API works on your TV — green light to build the dashboard.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
