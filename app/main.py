"""FastAPI application — routes for Docent."""
import hashlib
import io
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image

from . import db
from .config import settings
from .discovery import discover_tvs
from .imaging import prepare_for_frame
from .slideshow import Slideshow
from .tv import tv

db.init()

slideshow = Slideshow(tv)

# Tracks the most recently observed on-screen content_id so /status can fire
# an HX-Trigger when it changes (driven by slideshow ticks or remote-controlled
# changes), letting the library refresh only when it actually needs to.
_last_current_id: str | None = None

BASE_DIR = Path(__file__).parent

app = FastAPI(title="Docent")


# ---- local-only guard -----------------------------------------------------
# There's no auth, so only this Mac's own pages may drive the TV. The Host
# check stops DNS rebinding (a site re-resolving its name to 127.0.0.1); the
# Origin / Sec-Fetch-Site check stops other sites' forms, whose POSTs skip
# CORS preflight. SwiftBar and curl send neither header, so they pass.
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _hostname(netloc: str) -> str | None:
    try:
        return urlsplit(f"//{netloc}").hostname
    except ValueError:
        return None


def _same_origin(origin: str, host: str) -> bool:
    try:
        return urlsplit(origin).netloc == host
    except ValueError:
        return False


@app.middleware("http")
async def local_only(request: Request, call_next):
    host = request.headers.get("host", "")
    if _hostname(host) not in _LOCAL_HOSTS:
        return JSONResponse({"detail": "Docent only answers on 127.0.0.1."}, status_code=403)
    if request.method not in _SAFE_METHODS:
        origin = request.headers.get("origin")
        fetch_site = request.headers.get("sec-fetch-site")
        if (origin is not None and not _same_origin(origin, host)) or (
            fetch_site is not None and fetch_site not in ("same-origin", "none")
        ):
            return JSONResponse({"detail": "Cross-site request blocked."}, status_code=403)
    return await call_next(request)


app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def _time_ago(epoch_seconds: float | None) -> str:
    """Render an upload timestamp as '3 hr ago', 'yesterday', etc.
    Empty string for None/0 (use as a falsey signal in templates)."""
    if not epoch_seconds:
        return ""
    delta = max(0, int(time.time() - epoch_seconds))
    day = 86400
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{delta // 60} min ago"
    if delta < day:
        return f"{delta // 3600} hr ago"
    if delta < 2 * day:
        return "yesterday"
    if delta < 14 * day:
        return f"{delta // day} days ago"
    if delta < 60 * day:
        return f"{delta // (7 * day)} weeks ago"
    if delta < 365 * day:
        return f"{delta // (30 * day)} months ago"
    years = delta // (365 * day)
    return "1 year ago" if years == 1 else f"{years} years ago"


def _pretty_name(filename: str | None) -> str:
    """'Solitary_Ocean_Wave.png' → 'Solitary Ocean Wave'."""
    if not filename:
        return ""
    stem = re.sub(r"\.(jpe?g|png|heic|heif|webp|tiff?|gif|bmp)$", "", filename.strip(), flags=re.I)
    return re.sub(r"[_]+", " ", stem).strip() or filename


templates.env.filters["time_ago"] = _time_ago

# ---- matte vocabulary -----------------------------------------------------
# The TV reports raw ids (`modernthin`, `burgandy`); the UI shows these names.
MATTE_STYLE_NAMES = {
    "none": "None", "modernthin": "Modern Thin", "modern": "Modern",
    "modernwide": "Modern Wide", "flexible": "Flexible", "shadowbox": "Shadowbox",
    "panoramic": "Panoramic", "triptych": "Triptych", "mix": "Mix", "squares": "Squares",
}
MATTE_COLOR_NAMES = {
    "polar": "Polar", "neutral": "Neutral", "warm": "Warm", "antique": "Antique",
    "sand": "Sand", "apricot": "Apricot", "redorange": "Red Orange", "burgandy": "Burgundy",
    "byzantine": "Byzantine", "lavender": "Lavender", "skyblue": "Sky Blue",
    "turquoise": "Turquoise", "seafoam": "Seafoam", "sage": "Sage", "navy": "Navy",
    "black": "Black",
}
# Display order for the picker: light neutrals → warm → cool → dark.
_COLOR_ORDER = list(MATTE_COLOR_NAMES)
_STYLE_ORDER = list(MATTE_STYLE_NAMES)


def _style_name(key: str) -> str:
    return MATTE_STYLE_NAMES.get(key, key.replace("_", " ").title())


def _color_name(key: str) -> str:
    return MATTE_COLOR_NAMES.get(key, key.replace("_", " ").title())


def _split_matte_id(matte_id: str) -> tuple[str, str]:
    """Decompose a matte_id into (type, color). 'none' has no color so we
    return a default ('polar') so the user keeps a sensible color choice
    when switching from 'none' back to a colored type."""
    if not matte_id or matte_id == "none":
        return "none", "polar"
    if "_" in matte_id:
        t, c = matte_id.split("_", 1)
        return t, c
    return matte_id, "polar"


def _matte_options() -> dict:
    """Matte styles + colors the TV supports, ordered and labeled for the
    picker. Empty lists if the TV hasn't answered yet."""
    types = tv.matte_types()
    colors = tv.matte_colors()
    styles = sorted(types, key=lambda t: _STYLE_ORDER.index(t) if t in _STYLE_ORDER else 99)
    colors = sorted(colors, key=lambda c: _COLOR_ORDER.index(c["color"]) if c["color"] in _COLOR_ORDER else 99)
    return {
        "styles": [{"key": t, "name": _style_name(t)} for t in styles],
        "colors": [
            {"key": c["color"], "name": _color_name(c["color"]),
             "hex": "#{:02x}{:02x}{:02x}".format(c.get("R", 0), c.get("G", 0), c.get("B", 0))}
            for c in colors
        ],
    }


def _art_view(item: dict, meta: dict, colors: dict[str, str]) -> dict:
    """Everything a template needs to draw one artwork."""
    cid = item.get("content_id", "")
    m = meta.get(cid) or {}
    style, color = _split_matte_id(item.get("matte_id") or "")
    is_store = not cid.startswith("MY")
    name = _pretty_name(m.get("filename")) or ("Art Store piece" if is_store else cid)
    return {
        "id": cid,
        "name": name,
        "is_store": is_store,
        "style": style,
        "color": color,
        "matte_hex": colors.get(color, "#f2f2ee"),
        "matte_label": "No matte" if style == "none" else f"{_style_name(style)} · {_color_name(color)}",
        "uploaded_at": m.get("uploaded_at") or 0,
        "source_size": f"{m['source_width']} × {m['source_height']}" if m.get("source_width") else "",
    }


def _color_hexes(opts: dict) -> dict[str, str]:
    return {c["key"]: c["hex"] for c in opts["colors"]}


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


# ---- library --------------------------------------------------------------

def _library_ctx(
    current_id_override: str | None = None,
    sort: str = "newest",
) -> dict:
    items = [it for it in tv.available() if isinstance(it, dict)]
    stale = not items
    if stale:
        # TV asleep or busy — show what it had last time (the UI greys it out).
        items = [it for it in tv.last_known() if isinstance(it, dict)]
    current = None if stale else tv.current()
    current_id = current_id_override or (current.get("content_id") if current else None)
    meta = db.metadata_for([it.get("content_id", "") for it in items])
    opts = _matte_options()
    hexes = _color_hexes(opts)
    arts = [_art_view(it, meta, hexes) for it in items]

    if sort == "name":
        arts.sort(key=lambda a: a["name"].lower())
    elif sort == "current":
        # Currently on-screen first, then everything else by newest upload.
        arts.sort(key=lambda a: (a["id"] != current_id, -a["uploaded_at"], a["id"]))
    else:  # "newest" (default)
        # Newest upload first; untracked images (no DB row) sort last by content_id.
        arts.sort(key=lambda a: (-a["uploaded_at"], a["id"]))

    return {
        "arts": arts,
        "current_id": current_id,
        "matte": opts,
        "sort": sort,
        "stale": stale,
        "counts": {
            "all": len(arts),
            "mine": sum(1 for a in arts if not a["is_store"]),
            "store": sum(1 for a in arts if a["is_store"]),
        },
    }


def _library_response(request: Request, headers: dict | None = None, **ctx_kwargs):
    return templates.TemplateResponse(
        request, "partials/library.html", _library_ctx(**ctx_kwargs), headers=headers
    )


@app.get("/library", response_class=HTMLResponse)
def library(request: Request, sort: str = "newest"):
    return _library_response(request, sort=sort)


# ---- status / connection --------------------------------------------------

def _tv_state() -> str:
    """One of: 'no_ip' (first-run, no TV configured), 'pairing' (IP set, no
    token yet — the TV should be showing the pair prompt), 'connected', or
    'unreachable' (IP set, had a token, but TV doesn't answer)."""
    if not settings.tv_ip:
        return "no_ip"
    if tv.supported():
        # Opportunistically cache the MAC so we can WoL when the TV is off.
        if not settings.mac_file.exists():
            tv.cache_mac()
        return "connected"
    if not settings.token_file.exists():
        return "pairing"
    return "unreachable"


def _status_ctx() -> dict:
    state = _tv_state()
    connected = state == "connected"
    current = tv.current() if connected else None
    artmode = tv.artmode() if connected else None

    now = None
    if current and current.get("content_id"):
        cid = current["content_id"]
        # get_current() usually carries matte_id; fall back to the cached library.
        item = dict(current)
        if not item.get("matte_id"):
            item.update(next((it for it in tv.available_cached() if it.get("content_id") == cid), {}))
        now = _art_view(item, db.metadata_for([cid]), _color_hexes(_matte_options()))

    s = slideshow.status()
    return {
        "state": state,
        "tv_ip": settings.tv_ip,
        "artmode": artmode,
        "now": now,
        "slideshow": {"running": s.running, "interval": s.interval_min, "shuffle": s.shuffle},
        "has_mac": settings.mac_file.exists(),
    }


def _status_response(request: Request, headers: dict | None = None):
    global _last_current_id
    ctx = _status_ctx()
    response = templates.TemplateResponse(request, "partials/status.html", ctx, headers=headers)
    cur_id = ctx["now"]["id"] if ctx["now"] else None
    if cur_id != _last_current_id:
        _last_current_id = cur_id
        # Tell the library to refresh — picked up by hx-trigger="currentChanged from:body".
        response.headers["HX-Trigger"] = "currentChanged"
    return response


@app.get("/status", response_class=HTMLResponse)
def status(request: Request):
    return _status_response(request)


@app.get("/api/status")
def api_status():
    """Machine-readable status for the SwiftBar plugin."""
    ctx = _status_ctx()
    return JSONResponse({
        "state": ctx["state"],
        "reachable": ctx["state"] == "connected",
        "ip": ctx["tv_ip"],
        "artmode": ctx["artmode"],
        "showing": ctx["now"]["name"] if ctx["now"] else None,
        "showing_id": ctx["now"]["id"] if ctx["now"] else None,
        "slideshow": ctx["slideshow"],
    })


# Mutations that change TV state answer with HX-Trigger: refreshStatus so the
# status poller re-renders immediately instead of waiting for the next tick.
_REFRESH = {"HX-Trigger": "refreshStatus"}


@app.post("/wake")
def wake_tv():
    """Send a Wake-on-LAN magic packet. The TV must have been online once
    so the dashboard could cache its MAC, and 'Network Standby' must be
    enabled on the TV (Settings → General → Network → Expert Settings)."""
    if not tv.wake():
        raise HTTPException(
            status_code=400,
            detail="Can't wake the TV from here yet — turn it on with the remote once so the dashboard can learn its network address.",
        )
    return Response(status_code=204, headers=_REFRESH)


@app.post("/artmode")
def artmode_set(on: str = Form("1")):
    """Switch Art Mode on (show art) or off (regular TV)."""
    if not tv.set_artmode(on not in ("0", "false", "off", "")):
        raise HTTPException(status_code=502, detail="The TV didn't respond to the Art Mode change.")
    return Response(status_code=204, headers=_REFRESH)


@app.post("/power/off")
def power_off():
    """Full standby via KEY_POWER on the remote channel."""
    slideshow.stop()
    if not tv.power_off():
        raise HTTPException(status_code=502, detail="The TV didn't accept the power-off command.")
    return Response(status_code=204, headers=_REFRESH)


@app.post("/artmode/toggle", response_class=HTMLResponse)
def artmode_toggle(request: Request):
    """Legacy power toggle (used by the SwiftBar plugin). From Art Mode →
    full power off. From off / unreachable → Wake-on-LAN if we have a
    cached MAC, else set_artmode(True)."""
    cur = tv.artmode()
    if cur == "on":
        if not tv.power_off():
            raise HTTPException(status_code=502, detail="TV did not accept power off")
    else:
        if not tv.supported():
            if tv.wake():
                return _status_response(request)
            raise HTTPException(
                status_code=502,
                detail="TV unreachable (no cached MAC for Wake-on-LAN — bring the TV online manually once)",
            )
        if not tv.set_artmode(True):
            raise HTTPException(status_code=502, detail="TV did not accept set_artmode")
    return _status_response(request)


# ---- settings -------------------------------------------------------------

def _settings_response(request: Request, headers: dict | None = None):
    return templates.TemplateResponse(
        request, "partials/settings_panel.html",
        {"tv_ip": settings.tv_ip, "state": _tv_state()},
        headers=headers,
    )


@app.get("/settings", response_class=HTMLResponse)
def settings_panel(request: Request):
    return _settings_response(request)


_IP_OR_HOST = re.compile(r"^[A-Za-z0-9._-]{1,253}$")


@app.post("/settings/tv-ip", response_class=HTMLResponse)
def tv_ip_update(request: Request, tv_ip: str = Form("")):
    new_ip = tv_ip.strip()
    if not new_ip or not _IP_OR_HOST.match(new_ip):
        raise HTTPException(status_code=400, detail="That doesn't look like an IP address — it should look like 192.168.1.20.")
    settings.update_tv_ip(new_ip)
    tv.reconnect()
    return _settings_response(request, headers=_REFRESH)


@app.post("/pair/reset", response_class=HTMLResponse)
def pair_reset(request: Request):
    """Forget the pairing token so the next connect shows the Allow prompt
    on the TV again. Use when the TV stopped accepting this Mac."""
    settings.token_file.unlink(missing_ok=True)
    tv.reconnect()
    return _settings_response(request, headers=_REFRESH)


@app.get("/discover", response_class=HTMLResponse)
def discover(request: Request):
    """SSDP scan for Samsung TVs on the LAN. Slow (~3s) — only triggered
    from setup UI."""
    found = discover_tvs(timeout=3.0)
    return templates.TemplateResponse(
        request, "partials/discover_list.html", {"found": found, "tv_ip": settings.tv_ip}
    )


# ---- slideshow ------------------------------------------------------------

def _slideshow_response(request: Request, headers: dict | None = None):
    s = slideshow.status()
    return templates.TemplateResponse(
        request, "partials/slideshow_panel.html",
        {"running": s.running, "duration": s.interval_min or 10, "shuffle": s.shuffle},
        headers=headers,
    )


@app.get("/slideshow", response_class=HTMLResponse)
def slideshow_panel(request: Request):
    return _slideshow_response(request)


@app.post("/slideshow", response_class=HTMLResponse)
def slideshow_set(
    request: Request,
    duration: int = Form(0),
    shuffle: str | None = Form(None),
):
    """Start (duration > 0) or stop (duration=0) the app-side slideshow.
    `shuffle` is sent only when its checkbox is checked (HTML form behavior),
    so a missing value means False, not the default True."""
    if duration <= 0:
        slideshow.stop()
    else:
        slideshow.start(interval_min=duration, shuffle=shuffle is not None)
    return _slideshow_response(request, headers=_REFRESH)


@app.post("/slideshow/stop", response_class=HTMLResponse)
def slideshow_stop(request: Request):
    slideshow.stop()
    return _slideshow_response(request, headers=_REFRESH)


@app.post("/slideshow/toggle", response_class=HTMLResponse)
def slideshow_toggle(request: Request):
    """Capsule switch: stop if running, else start with the last settings."""
    s = slideshow.status()
    if s.running:
        slideshow.stop()
    else:
        slideshow.start(interval_min=s.interval_min or 10, shuffle=s.shuffle)
    return _status_response(request)


# ---- images ---------------------------------------------------------------

@app.post("/upload", response_class=HTMLResponse)
async def upload(request: Request, file: UploadFile = File(...)):
    """Accept a multipart image upload, pad to 3840x2160, push to TV,
    record metadata locally. If the same source bytes were already
    uploaded, activate the existing image instead of re-pushing.
    The resulting content_id comes back in the X-Content-Id header."""
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="That file is empty.")

    sha = hashlib.sha256(raw).hexdigest()

    # Dedup: if we've seen these bytes before AND the TV still has the image,
    # just re-activate it. (If the user deleted from the TV but our DB row
    # remains, fall through to re-upload.)
    existing_cid = db.find_by_hash(sha)
    if existing_cid and any(
        isinstance(it, dict) and it.get("content_id") == existing_cid
        for it in tv.available()
    ):
        tv.select_image(existing_cid)
        return _library_response(
            request,
            headers={"HX-Trigger": "duplicateUpload, refreshStatus", "X-Content-Id": existing_cid},
            current_id_override=existing_cid,
        )

    # Probe source dimensions before we throw the bytes through Pillow's
    # padding pipeline — we want to record the original size, not 3840x2160.
    try:
        with Image.open(io.BytesIO(raw)) as src:
            src_w, src_h = src.size
    except Exception:
        src_w, src_h = None, None

    try:
        prepared = prepare_for_frame(raw)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Couldn't read that image ({e}).")

    new_cid = tv.upload(prepared)
    if not new_cid:
        raise HTTPException(status_code=502, detail="The TV didn't accept the upload.")
    tv.invalidate_available_cache()

    db.record_upload(
        content_id=new_cid,
        filename=file.filename,
        sha256=sha,
        source_width=src_w,
        source_height=src_h,
        raw_bytes=len(raw),
    )

    return _library_response(
        request,
        headers={"HX-Trigger": "refreshStatus", "X-Content-Id": new_cid},
    )


@app.post("/image/{content_id}/rename", response_class=HTMLResponse)
def rename(request: Request, content_id: str, name: str = Form(""), sort: str = Form("newest")):
    """Persist a new display name to local DB. Empty/whitespace is ignored."""
    name = (name or "").strip()
    if name:
        db.set_filename(content_id, name)
    return _library_response(request, headers=_REFRESH, sort=sort)


@app.delete("/image/{content_id}", response_class=HTMLResponse)
def delete_image(request: Request, content_id: str, sort: str = "newest"):
    """Delete an image from the TV, drop its DB row, and invalidate its
    cached thumbnail. Returns the refreshed library."""
    if not tv.delete(content_id):
        raise HTTPException(status_code=502, detail="The TV didn't accept the delete.")
    tv.invalidate_available_cache()
    db.forget(content_id)
    cache_path = settings.thumb_dir / f"{content_id}.jpg"
    if cache_path.exists():
        cache_path.unlink()
    return _library_response(request, headers=_REFRESH, sort=sort)


@app.post("/image/{content_id}/activate", response_class=HTMLResponse)
def activate(request: Request, content_id: str, sort: str = Form("newest")):
    """Set the given content_id as the current Art Mode image, then return
    the refreshed library so the 'On TV' badge moves to the new tile."""
    if not tv.select_image(content_id):
        raise HTTPException(status_code=502, detail="The TV didn't respond — it may be asleep.")
    return _library_response(request, headers=_REFRESH, current_id_override=content_id, sort=sort)


def _apply_matte(content_id: str, matte_type: str | None, color: str | None) -> bool:
    """Change an image's matte if it differs from what the TV has. Returns
    True if a change was made. Raises HTTPException if the TV refuses."""
    types = set(tv.matte_types())
    colors = {c["color"] for c in tv.matte_colors()}

    items = {it["content_id"]: it for it in tv.available()}
    cur_matte = items.get(content_id, {}).get("matte_id", "")
    cur_type, cur_color = _split_matte_id(cur_matte)

    new_type = matte_type or cur_type
    new_color = color or cur_color

    if new_type not in types:
        raise HTTPException(status_code=400, detail=f"Unknown matte style: {new_type}")
    if new_color not in colors:
        raise HTTPException(status_code=400, detail=f"Unknown matte color: {new_color}")

    new_matte_id = "none" if new_type == "none" else f"{new_type}_{new_color}"
    if new_matte_id == cur_matte:
        return False

    if not tv.change_matte(content_id, new_matte_id):
        raise HTTPException(
            status_code=502,
            detail=f"Your TV won't use {_style_name(new_type)} with this image — "
                   "its shape doesn't fit that matte. Try Modern or Flexible.",
        )
    tv.invalidate_available_cache()
    return True


@app.post("/image/{content_id}/show", response_class=HTMLResponse)
def show(
    request: Request,
    content_id: str,
    matte_type: str | None = Form(default=None),
    color: str | None = Form(default=None),
    sort: str = Form("newest"),
):
    """The preview's 'Show on TV': apply the chosen matte (if changed),
    then display the image."""
    _apply_matte(content_id, matte_type, color)
    # select_image also forces the TV to redraw with the new matte when the
    # image was already on screen.
    if not tv.select_image(content_id):
        raise HTTPException(status_code=502, detail="The TV didn't respond — it may be asleep.")
    return _library_response(request, headers=_REFRESH, current_id_override=content_id, sort=sort)


@app.post("/image/{content_id}/matte", response_class=HTMLResponse)
def change_matte(
    request: Request,
    content_id: str,
    matte_type: str | None = Form(default=None),
    color: str | None = Form(default=None),
    sort: str = Form("newest"),
):
    """Change the matte on a single image without displaying it. Accepts
    `matte_type`, `color`, or both — whichever isn't supplied is preserved."""
    _apply_matte(content_id, matte_type, color)
    # change_matte updates the metadata; force a redraw if it's the one on screen.
    cur = tv.current()
    if cur and cur.get("content_id") == content_id:
        tv.select_image(content_id)
    return _library_response(request, headers=_REFRESH, sort=sort)


@app.get("/thumb/{content_id}")
def thumb(content_id: str):
    """Serve a thumbnail. Cached on disk after first fetch from the TV."""
    cache_path = settings.thumb_dir / f"{content_id}.jpg"
    headers = {"Cache-Control": "max-age=3600"}
    if cache_path.exists():
        return Response(cache_path.read_bytes(), media_type="image/jpeg", headers=headers)

    data = tv.thumbnail(content_id)
    if not data:
        raise HTTPException(status_code=404, detail="thumbnail not available")

    cache_path.write_bytes(data)
    return Response(data, media_type="image/jpeg", headers=headers)
