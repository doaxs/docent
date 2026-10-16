# CLAUDE.md

Guidance for Claude Code (and humans) working on **Docent** — a local web dashboard for Art Mode on Samsung Frame TVs.

Personal, machine-specific notes go in `CLAUDE.local.md` (gitignored; Claude Code loads it alongside this file).

## Project overview

A read-and-write web UI that runs on a Mac on the same network as the TV: see what's displayed, browse the library, upload photos, change the active art, set mattes, and run slideshows. It runs as a dev server (`./run.sh`, port 8000), as a native window (`Docent.app` via `desktop.py`, port 8765), and has an optional SwiftBar menu bar plugin.

**Local-only by design.** No cloud, no auth, no public exposure. Single user, single TV, single LAN. The `local_only` middleware in `app/main.py` enforces it: requests must carry a `127.0.0.1`/`localhost` Host, and mutating requests from another browser origin get a 403. Keep it — without auth, it's what stops any web page from driving the TV.

## Tested hardware

Built against a Frame Pro 2026 (`QN55LS03HWFXZA`) (quirks first probed with `samsungtvws==3.0.4`; pinned to 3.0.6). Should work on any Frame with the Tizen WebSocket API (2021+). See `docs/FIRMWARE.md` for where that firmware diverges from the library.

## Stack

- **Backend**: FastAPI + Jinja2 templates, sync (not async — the underlying `samsungtvws` is sync)
- **TV API**: `samsungtvws==3.0.6`, pinned exactly (the `xchwarze/samsung-tv-ws-api` Python library — unofficial WebSocket wrapper around Tizen's Smart TV API)
- **Frontend**: Server-rendered HTML with HTMX 2.0.4 for live partials (no SPA, no build step, no npm)
- **Image processing**: Pillow
- **Storage**: `data/` (gitignored) — thumbnail cache, plain `.txt` pairing tokens, `dashboard.sqlite` for upload metadata (`app/db.py`)
- **Config**: pydantic-settings reading from `.env`
- **Server**: uvicorn with `--reload`
- **Desktop app**: pywebview window around the same server, packaged with PyInstaller (`Docent.spec`, `./build_app.sh`)

### Design decisions

- **Sync, not async.** `samsungtvws`'s sync API is the well-trodden one, and FastAPI runs sync routes on a threadpool. With one user and one TV there's nothing to gain from async.
- **HTMX, not a SPA.** Live partial updates with no build step, no state library, and no separate API contract.
- **Runs on your Mac, not a Pi/NAS or container.** Keep it simple to install with a venv.

Don't propose switching to a SPA or containerizing without a strong reason.

## Common commands

```bash
# Setup (one-time, from the repo root)
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt   # requirements-desktop.txt for Docent.app
cp .env.example .env              # optional — the UI can scan for the TV

# Run
source venv/bin/activate
./run.sh
# Dashboard at http://127.0.0.1:8000

# Re-pair if TV-side rejects the stored token
rm -f data/tv_token.txt
# then ./run.sh again — accept the prompt on the TV screen within ~30s

# UI work without a TV — fake TV, library from $DATA_DIR/thumbs/*.jpg
python tools/fetch_demo_art.py            # once: public-domain paintings into demo-data/
DOCENT_DEMO=1 DATA_DIR=demo-data ./run.sh

# Desktop app
./build_app.sh && open dist/Docent.app
```

Standalone diagnostic scripts (kept in the repo, useful when debugging):

- `tools/frame_check.py` — runs through 9 sequential API calls end-to-end. Use to confirm the TV is reachable and the API works before assuming the dashboard itself is broken.
- `tools/probe_thumbs.py` — lists every method on `art()` matching `thumb` and tries each to see what works on the installed library version.

## Project structure

```
docent/
├── app/
│   ├── __init__.py
│   ├── main.py              # FastAPI routes
│   ├── config.py            # Settings via .env (pydantic-settings)
│   ├── tv.py                # Singleton wrapper around samsungtvws — all TV calls go through here
│   ├── demo.py              # Fake TV for UI work (DOCENT_DEMO=1) — built from data/thumbs/
│   ├── static/
│   │   ├── style.css        # macOS 27 Liquid Glass styling
│   │   └── app.js           # Client-only behavior: preview, matte tray, filter/search, upload, dialogs
│   └── templates/
│       ├── base.html        # Layout, HTMX script, theme bootstrap
│       ├── index.html       # Window shell: toolbar, TV stage, library grid (under the TV or full-window), popovers
│       └── partials/        # HTMX-swappable HTML fragments
│           ├── status.html          # /status — all OOB swaps (TV screen, info column, capsule)
│           ├── library.html         # /library — tiles + OOB counts/matte data
│           ├── slideshow_panel.html # slideshow popover
│           ├── settings_panel.html  # TV address / scan / pair again / appearance popover
│           ├── discover_list.html   # SSDP scan results
│           ├── macros.html          # icon() + matte() (matte() mirrors matteHTML() in app.js)
│           └── icons.html           # SF-Symbols-style SVG sprite
├── data/                    # gitignored — runtime state
│   ├── tv_token.txt         # pairing token (auto-created on first connect)
│   └── thumbs/              # disk-cached thumbnails by content_id
├── swiftbar/                # menu bar plugin (docent.10s.py)
├── launchd/                 # optional LaunchAgent (template paths)
├── tools/                   # frame_check.py, probe_thumbs.py diagnostics; fetch_demo_art.py
├── desktop.py               # Docent.app entry point (pywebview); build with ./build_app.sh
├── docs/                    # FIRMWARE.md, MENUBAR.md, images/ (README screenshots)
├── .env                     # local config (gitignored)
├── .env.example
├── requirements.txt
├── run.sh
└── README.md
```

## Key conventions and gotchas

### TV calls always go through `app/tv.py`
The `TVClient` singleton wraps every `samsungtvws` call in a try/except. On any exception it logs a warning, closes the connection, and resets — so the next call attempts a fresh handshake. This handles the common case of the TV going to sleep and waking back up. **Don't bypass this wrapper** — calling `samsungtvws` directly in route handlers will introduce hangs.

### Timeouts are critical
The `samsungtvws` library has no default timeout. Without one, a TV in deep standby or a stale token causes routes to hang forever. The TV client passes `timeout=8` to the constructor. Keep that — never go higher than ~10s for a UI-facing call.

### Token invalidation when network changes
If the TV's IP changes (or the user moves between networks), the existing `data/tv_token.txt` may silently fail handshake without raising an exception — the websocket connect succeeds but no further messages come back. **Symptom**: requests hang for the full timeout, then fall through to "TV unreachable". **Fix**: delete the token file and re-pair via the on-TV prompt.

### Content ID conventions
- `MY_*` — personal uploaded photos
- `SAM-*` — Samsung Art Store content
The dashboard badges these differently. The `available()` call returns dicts with `content_id`, `category_id`, `content_type`, sometimes `matte_id` and `portrait_matte_id`. Sometimes returns plain strings on older firmware — `tv.py` normalizes both shapes to dicts.

### Thumbnails
- Use `art().get_thumbnail_list([cid])` — the single-fetch `get_thumbnail()` hangs indefinitely on the 2026 firmware (D2D socket never responds, constructor `timeout=` not honored on that path). The batch endpoint accepts a single id and works in ~1s.
- Cache hits to `data/thumbs/{content_id}.jpg`. Don't re-fetch from TV on every page load.

### HTMX, not JS frameworks
Don't reach for React, Vue, Alpine.js for new features unless we hit a wall HTMX can't solve. The pattern is: server route returns an HTML fragment, an `hx-*` attribute swaps it into the DOM. Keep partials in `app/templates/partials/`.

- **Server owns TV state.** A hidden `#status-poll` (`hx-trigger="load, every 10s, refreshStatus from:body"`, `hx-swap="none"`) fetches `/status`, whose response is only OOB swaps: `#tv-state`, `#screen-now`, `#info-now`, `#capsule`, `#tb-sub`. `app.js` cancels an OOB swap when the incoming `data-key` matches the current one so the poll doesn't restart animations. Mutating routes return `HX-Trigger: refreshStatus` for an immediate re-render.
- **Browser owns the preview.** Clicking art previews it on the TV render (`#screen-preview`, `#info-preview`, matte tray) — nothing hits the TV until **Show on TV** (`POST /image/{id}/show` applies matte + select). These flows use `fetch` + `htmx.swap('#library', …)` in `app.js` because they coordinate client state.
- **Library** is one set of tiles in a vertically scrolling grid — under the TV (`data-view="tv"`, default) or full-window with a sidebar (`data-view="grid"`), remembered in localStorage. Filter/search are client-side off tile `data-*` attributes; sort is server-side.
- **UI work without the TV:** `DOCENT_DEMO=1` swaps in `app/demo.py` (fake library from `$DATA_DIR/thumbs/`; `tools/fetch_demo_art.py` fills `demo-data/`). Never point demo tooling at `data/` — those thumbnails belong to the real TV. `DOCENT_DEMO_STATE=unreachable|pairing` pins a connection state. Never test UI changes against the real TV — it changes what's on the wall.
- The SwiftBar plugin reads `/api/status` (JSON). Don't make it scrape HTML again.

### Image upload pipeline (in `app/imaging.py` + `app/main.py`)
Source bytes → SHA-256 hash → dedup check against SQLite → if not seen, EXIF-aware decode + `ImageOps.contain` to 3840×2160 + black-pad + JPEG quality 92 → `art.upload(bytes, file_type="JPEG", matte="flexible_polar", ...)` → record `(content_id, filename, sha, dims)` in `data/dashboard.sqlite`. Default matte must be `flexible_*` (not the library's `shadowbox_polar` default) — see `docs/FIRMWARE.md`.

### Concurrency
`TVClient` serializes all TV calls through a `threading.Lock` with a 4s acquire timeout. The Tizen WS API is single-channel and concurrent calls from uvicorn worker threads can wedge the connection. Callers that can't acquire fail fast with the method's default value (False / None / []) rather than tying up an HTTP worker.

### Firmware quirks
The 2026 firmware diverges from samsungtvws's documented behavior in several places (broken thumbnail and slideshow endpoints, duplicate library entries, aspect-ratio gating on mattes, stale current-image ids). All documented in `docs/FIRMWARE.md` — add new ones there. Re-run `tools/probe_thumbs.py` after firmware updates.
