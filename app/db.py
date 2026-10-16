"""SQLite metadata for uploaded images.

The Frame's library only knows opaque content_ids like `MY_F0017`. We layer
on local metadata so the dashboard can show original filenames, detect
duplicate uploads (same source bytes → same content_id, no re-push), and
later support collections/schedules/watched-folder sync.

Connections are short-lived (open per call) — fine for a single-user LAN
dashboard with low write volume. SQLite handles concurrent readers itself.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import settings

log = logging.getLogger(__name__)

DB_PATH: Path = settings.data_dir / "dashboard.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS uploads (
    content_id      TEXT PRIMARY KEY,
    filename        TEXT,
    sha256          TEXT NOT NULL,
    uploaded_at     REAL NOT NULL,
    source_width    INTEGER,
    source_height   INTEGER,
    bytes           INTEGER
);
CREATE INDEX IF NOT EXISTS uploads_sha256_idx ON uploads(sha256);
"""


def init() -> None:
    """Create schema on first run. Idempotent."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _conn() as c:
        c.executescript(SCHEMA)


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(DB_PATH, timeout=5)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def record_upload(
    *,
    content_id: str,
    filename: str | None,
    sha256: str,
    source_width: int | None,
    source_height: int | None,
    raw_bytes: int | None,
) -> None:
    with _conn() as c:
        c.execute(
            """
            INSERT OR REPLACE INTO uploads
            (content_id, filename, sha256, uploaded_at, source_width, source_height, bytes)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (content_id, filename, sha256, time.time(), source_width, source_height, raw_bytes),
        )
    log.info(f"recorded upload: {content_id} <- {filename!r} ({sha256[:12]}…)")


def set_filename(content_id: str, filename: str) -> None:
    """Set the human-readable name for an image. Works for images uploaded
    outside the dashboard (no sha/dimensions known) — UPSERT preserves any
    existing metadata while overwriting just the filename."""
    with _conn() as c:
        c.execute(
            """
            INSERT INTO uploads (content_id, filename, sha256, uploaded_at)
            VALUES (?, ?, '', ?)
            ON CONFLICT(content_id) DO UPDATE SET filename = excluded.filename
            """,
            (content_id, filename, time.time()),
        )


def forget(content_id: str) -> None:
    """Drop the metadata row for a deleted image. Idempotent."""
    with _conn() as c:
        c.execute("DELETE FROM uploads WHERE content_id = ?", (content_id,))


def find_by_hash(sha256: str) -> str | None:
    """Return the content_id for a previously-uploaded image with this hash,
    or None if we haven't seen it. Used to short-circuit duplicate uploads."""
    with _conn() as c:
        row = c.execute(
            "SELECT content_id FROM uploads WHERE sha256 = ? ORDER BY uploaded_at DESC LIMIT 1",
            (sha256,),
        ).fetchone()
    return row["content_id"] if row else None


def metadata_for(content_ids: list[str]) -> dict[str, dict]:
    """Bulk lookup metadata for a list of content_ids."""
    if not content_ids:
        return {}
    placeholders = ",".join("?" * len(content_ids))
    with _conn() as c:
        rows = c.execute(
            f"SELECT * FROM uploads WHERE content_id IN ({placeholders})",
            content_ids,
        ).fetchall()
    return {row["content_id"]: dict(row) for row in rows}
