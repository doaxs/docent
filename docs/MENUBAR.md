# Run Docent headless + add a menu bar widget (advanced, macOS-only)

> Optional. The basic flow — `./run.sh` in a terminal, browser at
> <http://127.0.0.1:8000> — works for everyone with no setup beyond pip install.
> This page is for users who want the dashboard always running in the
> background with a menu bar status icon.

Goal: never open a terminal again. The dashboard runs in the background from login; a menu bar icon shows live TV status and gives one-click access.

Two independent pieces — install whichever you want, in either order:

1. **launchd LaunchAgent** — auto-starts the FastAPI server at login, restarts on crash.
2. **SwiftBar plugin** — menu bar icon with status + quick actions.

Replace `~/path/to/docent` below with the absolute path to your local clone.

---

## 1. Auto-start the dashboard on login (launchd)

```bash
# One-time install: copy the plist into LaunchAgents and load it.
cp ~/path/to/docent/launchd/com.frame.dashboard.plist \
   ~/Library/LaunchAgents/

launchctl load ~/Library/LaunchAgents/com.frame.dashboard.plist

# Verify it's running.
curl -sS http://127.0.0.1:8000/status | head -1
```

After that, the dashboard is always available at <http://127.0.0.1:8000>. It restarts automatically if it ever crashes, and starts at every login.

**Common operations:**

```bash
# Stop the agent (e.g. for maintenance)
launchctl unload ~/Library/LaunchAgents/com.frame.dashboard.plist

# Restart (after editing code)
launchctl unload ~/Library/LaunchAgents/com.frame.dashboard.plist && \
launchctl load   ~/Library/LaunchAgents/com.frame.dashboard.plist

# See what it's logging
tail -f data/launchd.out.log data/launchd.err.log

# Permanently remove
launchctl unload ~/Library/LaunchAgents/com.frame.dashboard.plist
rm ~/Library/LaunchAgents/com.frame.dashboard.plist
```

**Heads up:** the plist has `--reload` removed (intentional — file watchers in a daemonized process restart the agent on every code edit, which fights with launchd). When developing, unload the agent and run `./run.sh` manually so you keep the auto-reload behavior. Reload the agent when done.

### macOS Full Disk Access gotcha

If your project lives anywhere under `~/Documents/`, `~/Desktop/`, or
`~/Downloads/`, macOS's TCC privacy layer will block the launchd-spawned
Python from even reading your venv's `pyvenv.cfg`. You'll see this in
`data/launchd.err.log`:

```
PermissionError: [Errno 1] Operation not permitted: '.../venv/pyvenv.cfg'
```

The launchd job will keep crashing in a backoff loop. Two options:

**Option A — keep the project where it is, grant FDA to the venv's Python.**
1. System Settings → Privacy & Security → **Full Disk Access** → `+`
2. `⌘⇧G` and paste the *real* binary path (not the symlink) — find it with:
   ```bash
   readlink venv/bin/python3
   # e.g. /Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13
   ```
3. Add that file, toggle on, and reload the agent.

Note: this grants FDA to your system Python interpreter, which means *any*
script run with that interpreter inherits FDA. If that's too broad, use
option B, or install a project-local Python (via `pyenv` or a separate
python.org install) used only by this project's venv and grant FDA to that
binary instead.

**Option B — move the project out of TCC-protected folders.**
Move the repo to e.g. `~/docent/`, update the three paths in the
plist, and reload. No FDA needed.

---

## 2. Menu bar icon (SwiftBar)

Install SwiftBar (free, open source, ~5 MB):

```bash
brew install --cask swiftbar
open -a SwiftBar
```

On first launch SwiftBar asks for a plugins folder. Point it at this repo's
`swiftbar/` folder, or symlink the plugin into a folder you already use:

```bash
mkdir -p ~/Library/Application\ Support/SwiftBar/Plugins
ln -s ~/path/to/docent/swiftbar/docent.10s.py \
      ~/Library/Application\ Support/SwiftBar/Plugins/
```

An artwork-frame icon with a status dot appears in your menu bar. It updates every 10 seconds.

The plugin talks to whichever Docent server is running: **Docent.app** (port 8765)
first, then the dev server from `./run.sh` (port 8000).

### What the menu bar gives you

- **Icon dot** — green when the TV is on, orange when it's asleep or waiting
  for you to press Allow, gray when Docent isn't running.
- **Click** opens a menu with:
  - The TV's state (*On · Art Mode*, *On · Slideshow*, *On · Watching TV*, *Asleep*) and IP
  - A thumbnail and the name of the artwork on screen
  - **Art Mode** and **Slideshow** toggles (checkmark = on). Slideshow resumes
    with the interval and shuffle you last used in Docent.
  - **Wake TV** when it's asleep
  - **Open Docent** — brings up the app, or opens the dev server in your browser
  - **Refresh**

If no server is running, the menu offers **Open Docent** and **Start Dev Server in Terminal**.

### Tweaking the plugin

- Filename interval — `docent.10s.py` polls every 10 seconds. Rename to `docent.30s.py` if you'd rather it be lighter on the TV. SwiftBar's filename convention is `<name>.<interval><unit>.<ext>`. Units: `s`, `m`, `h`, `d`.
- Add more quick actions — add an `action(...)` line in `main()` and a matching branch in `run_action()`.
- Test without SwiftBar — run the plugin in a terminal to print its menu. Point it at a demo server with `DOCENT_URL=http://127.0.0.1:8001 ./swiftbar/docent.10s.py`.

---

## Common path issues

The launchd plist and SwiftBar plugin both contain absolute paths. Edit them to match your local clone:

- `launchd/com.frame.dashboard.plist` — three places: `ProgramArguments[0]`, `WorkingDirectory`, `StandardOutPath`/`StandardErrorPath`.
- `swiftbar/docent.10s.py` — `APP_URL` / `DEV_URL` only if you change the ports. The plugin gets its own absolute path from `sys.argv[0]` so the file's location can change without breaking the action callbacks.

---

## Why not a proper Mac app?

A real menu bar app (Swift + AppKit + maybe SwiftUI) would:
- Show real SF Symbols and richer UI (status icons, popover with images)
- Avoid the SwiftBar dependency
- Not need a separate scripting host

But it's hours of Swift to do well, and SwiftBar gets you ~95% of the same UX for the cost of one Python file. If you outgrow SwiftBar, the migration target is clear: a SwiftUI `MenuBarExtra` app that hits the same `/status` and `/artmode/toggle` endpoints we already have.
