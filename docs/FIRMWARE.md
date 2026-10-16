# Firmware quirks

Samsung's Tizen WebSocket API is unofficial and changes between firmware
versions. These are the places where it diverges from what
[`samsungtvws`](https://github.com/xchwarze/samsung-tv-ws-api) documents.

Observed on a **Frame Pro 2026, 55" (QN55LS03HWFXZA)**. First probed with
`samsungtvws==3.0.4`; Docent now pins 3.0.6, and the workarounds below are
still in use with it. Other models and firmware versions may behave
differently. If yours does, please open an issue (see *Contributing* in the
README).

To check your own TV, run the diagnostics in `tools/`:

```bash
source venv/bin/activate
python tools/frame_check.py --ip 192.168.1.20   # end-to-end: status, upload, select, delete
python tools/probe_thumbs.py                    # tries every thumbnail call path
```

Re-run them after a firmware update.

## Broken

### `art().get_thumbnail(cid)` hangs

The single-image call hangs forever on the D2D socket read, and the
constructor's `timeout=` isn't honored on that path.
`get_thumbnail_list([cid])` returns the same JPEG in about a second.

**In Docent:** `app/tv.py` `thumbnail()` always uses the list form.

### `art().set_slideshow_status(...)` returns error -7

"Invalid parameter" for every shape tried: all categories and durations,
with and without `sub_category_id`, `content_list`, `current_content_id` and
`type`. The newer `set_auto_rotation_status` times out (the TV doesn't
recognize the request). `get_slideshow_status` works but always reports
`off`.

**In Docent:** `app/slideshow.py` runs the slideshow itself, calling
`select_image()` every N minutes from a background thread. It rotates
personal photos (`MY_*`) only. The catch is that the Mac has to stay awake.
If Samsung fixes the native call, switching back would let the TV run
slideshows on its own.

## Partly working

### `art().change_matte(...)` depends on the image's aspect ratio

`flexible_*` mattes work on any image. Other types (`none`, `modern_*`, …)
return -7 unless the source is exactly 16:9.

**In Docent:** uploads are padded to 3840×2160, so they accept every matte,
and new uploads default to `flexible_polar`. The library's own default,
`shadowbox_polar`, isn't safe. Art that was already on the TV may only
accept `flexible_*` and `shadowbox_*`.

### `art().available()` returns duplicates

The same `content_id` appears more than once, apparently once per category
it belongs to.

**In Docent:** `app/tv.py` `available()` de-duplicates on `content_id`. It
also normalizes the older plain-string shape to dicts.

### Personal photos live in category `MY-C0008`

`samsungtvws` assumes `MY-C0002` ("My Pictures"). On this TV, uploads land
in `MY-C0008`.

Docent doesn't depend on this, because its slideshow doesn't use the native
API. Anything that calls `set_slideshow_status` needs the right id, so read it
from `available()` instead of hard-coding it.

### `art().get_current()` can point at deleted art

The current-image pointer isn't always in sync with the library. After a
delete it can still report the old `content_id`.

**In Docent:** `app/tv.py` `current()` checks the id against a short-cached
`available()` and ignores it if the image no longer exists.

## Confirmed working

`supported()`, `available()`, `get_current()`, `get_artmode()`,
`set_artmode()`, `upload()` (about 5 s round trip), `select_image()`,
`delete_list()`.

## Not yet tested

Brightness sensor, motion timer, and photo filters. Don't assume they work.

## Connection gotchas

- **Always pass a timeout.** `samsungtvws` has no default, so a TV in deep
  standby or a stale token hangs the call forever. Docent uses 8 s.
- **One call at a time.** Concurrent calls can wedge the connection. Docent
  serializes them with a lock.
- **A stale token fails silently.** After the TV's IP changes, the socket
  can connect and then never answer. Delete `data/tv_token.txt` and accept
  the pairing prompt on the TV again.
- **Remote keys use a separate token.** Power off goes over the encrypted
  remote channel (port 8002), which the TV authorizes separately from Art
  Mode. Docent stores that token in `data/tv_remote_token.txt`.
