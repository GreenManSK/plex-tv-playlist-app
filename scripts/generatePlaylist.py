#!/usr/bin/env python3
"""
generatePlaylist.py

Usage:
  python generatePlaylist.py <playlist_ratingKey> [--skip-watched] [--as-user <id-or-username>]

Purpose:
  Clears the specified Plex playlist and re-populates it in a round-robin order
  using episodes stored in SQLite (table: playlistEpisodes), grouped by timeSlot.
  Shows sharing a timeSlot play back-to-back, ordered by slotPriority.
  With --skip-watched, episodes already watched in Plex are left out.
  Playlists belong to a single Plex account, so the work is done as the
  `watched_user` setting (or --as-user) when one is set.

Environment:
  - .env in project root with:
      PLEX_URL
      PLEX_TOKEN
      PLEX_VERIFY_SSL (optional; default "false")

Requirements:
  - Tables populated by populateShows.py and getEpisodes.py

Exit codes:
  2 -> .env missing or PLEX_* missing
  3 -> Plex connection failed
  4 -> Playlist fetch failed or not a playlist
  5 -> SQLite DB missing or cannot open
  6 -> Failed to clear playlist
  7 -> Failed to add items
  0 -> Success
"""

import os
import sys
import sqlite3
import argparse
import time
from typing import Dict, List, Iterable
from urllib.parse import urlparse, urlunparse

import requests
from dotenv import load_dotenv
from plexapi.server import PlexServer
from plexapi.playlist import Playlist

# ---------------------------
# Paths & .env loading
# ---------------------------
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
ENV_PATH = os.path.join(ROOT, '.env')
DB_PATH = os.path.join(ROOT, 'database', 'plex_playlist.db')

if not os.path.exists(ENV_PATH):
    print(f"[ERROR] .env not found at {ENV_PATH}", file=sys.stderr)
    sys.exit(2)

load_dotenv(ENV_PATH, override=True)

PLEX_URL = os.getenv('PLEX_URL', '').strip()
PLEX_TOKEN = os.getenv('PLEX_TOKEN', '').strip()
PLEX_VERIFY_SSL = os.getenv('PLEX_VERIFY_SSL', 'false').strip().lower() in ('1', 'true', 'yes')

if not PLEX_URL or not PLEX_TOKEN:
    print(f"[ERROR] Missing PLEX_URL or PLEX_TOKEN in {ENV_PATH}", file=sys.stderr)
    sys.exit(2)

def remap_localhost_for_container(url: str) -> str:
    """Map localhost/127.0.0.1 to host.docker.internal for container -> host access."""
    try:
        u = urlparse(url or '')
        host = (u.hostname or '').lower()
        if host in ('localhost', '127.0.0.1'):
            scheme = (u.scheme or 'http')
            port = u.port or (443 if scheme == 'https' else 32400)
            netloc = f"host.docker.internal:{port}"
            return urlunparse((scheme, netloc, u.path or '', u.params or '', u.query or '', u.fragment or ''))
    except Exception:
        pass
    return url

# Remap if needed
PLEX_URL = remap_localhost_for_container(PLEX_URL)

# ---------------------------
# Args
# ---------------------------
parser = argparse.ArgumentParser(description="Clear and repopulate a Plex playlist from DB.")
parser.add_argument("ratingKey", type=int, help="The ratingKey (numeric id) of the target playlist")
parser.add_argument(
    "--skip-watched",
    action="store_true",
    help="Exclude episodes already marked as watched in Plex",
)
parser.add_argument(
    "--as-user",
    default=None,
    help="Plex Home user id/username that owns the playlist (overrides the stored setting)",
)
args = parser.parse_args()
playlist_rating_key: int = args.ratingKey
skip_watched: bool = args.skip_watched
playlist_user: str = (args.as_user or '').strip()

# ---------------------------
# Helpers
# ---------------------------
started_at = time.monotonic()

def format_duration(seconds: float) -> str:
    """Format an elapsed duration for progress messages."""
    minutes, remaining_seconds = divmod(max(0, int(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {remaining_seconds}s"
    if minutes:
        return f"{minutes}m {remaining_seconds}s"
    return f"{remaining_seconds}s"

def log(message: str, level: str = "INFO") -> None:
    """Write an immediately visible, timestamped progress message."""
    elapsed = format_duration(time.monotonic() - started_at)
    print(f"[{level}] [+{elapsed}] {message}", flush=True)

def round_robin(grouped: Dict[int, List[int]]) -> List[int]:
    """
    Interleave lists by index to produce a round-robin order.
    grouped = { timeSlot: [ratingKey, ...], ... }
    """
    if not grouped:
        return []
    keys = sorted(grouped.keys())
    max_len = max(len(v) for v in grouped.values()) if grouped else 0
    order: List[int] = []
    for i in range(max_len):
        for k in keys:
            lst = grouped.get(k, [])
            if i < len(lst):
                order.append(lst[i])
    return order

def chunked(iterable: List, size: int) -> Iterable[List]:
    """Yield lists of length <= size from iterable."""
    for i in range(0, len(iterable), size):
        yield iterable[i:i+size]

# ---------------------------
# Connect to Plex (requests.Session controls SSL verify)
# ---------------------------
stage_started_at = time.monotonic()
log(f"Connecting to Plex at {PLEX_URL}.")
try:
    session = requests.Session()
    session.verify = True if PLEX_VERIFY_SSL else False
    plex = PlexServer(PLEX_URL, PLEX_TOKEN, session=session)
except Exception as e:
    print(f"[ERROR] Failed to connect to Plex at {PLEX_URL}: {e}", file=sys.stderr)
    sys.exit(3)
log(f"Connected to Plex in {format_duration(time.monotonic() - stage_started_at)}.")

# ---------------------------
# Work as the account that owns the playlist
# ---------------------------
if not playlist_user and os.path.exists(DB_PATH):
    settings_conn = None
    try:
        settings_conn = sqlite3.connect(DB_PATH)
        setting_row = settings_conn.execute(
            "SELECT value FROM settings WHERE key = 'watched_user'"
        ).fetchone()
        playlist_user = (setting_row[0] or '').strip() if setting_row else ''
    except sqlite3.Error:
        playlist_user = ''
    finally:
        if settings_conn is not None:
            settings_conn.close()

if playlist_user:
    try:
        plex = plex.switchUser(playlist_user)
        log(f"Building the playlist as Plex user '{playlist_user}'.")
    except Exception as e:
        print(
            f"[ERROR] Could not switch to Plex user '{playlist_user}': {e}. "
            "Use the user's Plex Home name; users with a PIN cannot be used.",
            file=sys.stderr,
        )
        sys.exit(3)

# ---------------------------
# Fetch playlist by ratingKey
# ---------------------------
try:
    stage_started_at = time.monotonic()
    log(f"Fetching target playlist ratingKey={playlist_rating_key}.")
    item = plex.fetchItem(playlist_rating_key)
    if not isinstance(item, Playlist):
        print("[ERROR] The fetched item is not a Playlist. Check the ratingKey.", file=sys.stderr)
        sys.exit(4)
    playlist: Playlist = item
    log(
        f"Fetched target playlist '{playlist.title}' in "
        f"{format_duration(time.monotonic() - stage_started_at)}."
    )
except Exception as e:
    print(f"[ERROR] Could not fetch playlist with ratingKey {playlist_rating_key}: {e}", file=sys.stderr)
    sys.exit(4)

# ---------------------------
# Connect to DB and read episodes
# ---------------------------
if not os.path.exists(DB_PATH):
    print(f"[ERROR] Database not found at {DB_PATH}", file=sys.stderr)
    sys.exit(5)

try:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
except Exception as e:
    print(f"[ERROR] Could not open SQLite DB at {DB_PATH}: {e}", file=sys.stderr)
    sys.exit(5)

try:
    stage_started_at = time.monotonic()
    log(
        "Reading episode order from the database"
        + (" (skipping watched episodes)." if skip_watched else ".")
    )
    query = """
    SELECT ratingKey, timeSlot
    FROM playlistEpisodes
    {where}
    ORDER BY timeSlot, COALESCE(slotPriority, 1), show_id,
             CASE WHEN COALESCE(season, 0) = 0 THEN 1 ELSE 0 END,
             season, episode
    """.format(where="WHERE COALESCE(watchedStatus, 0) = 0" if skip_watched else "")
    cur.execute(query)
    rows = cur.fetchall()
finally:
    cur.close()
    conn.close()

if not rows:
    print("[WARN] No episodes found in playlistEpisodes. Nothing to add.", file=sys.stderr)
    rows = []

# Group by timeSlot
episodes_by_slot: Dict[int, List[int]] = {}
for rating_key, slot in rows:
    try:
        rk_int = int(rating_key)
        episodes_by_slot.setdefault(int(slot), []).append(rk_int)
    except Exception:
        continue

# Produce round-robin order
episode_order: List[int] = round_robin(episodes_by_slot)
log(
    f"Prepared {len(episode_order)} episodes across {len(episodes_by_slot)} timeslots in "
    f"{format_duration(time.monotonic() - stage_started_at)}."
)

# ---------------------------
# Clear existing items
# ---------------------------
try:
    stage_started_at = time.monotonic()
    log("Reading the existing playlist contents.")
    current_items = playlist.items()
    if current_items:
        log(f"Clearing {len(current_items)} existing playlist items.")
        playlist.removeItems(current_items)
        log(
            f"Cleared {len(current_items)} items in "
            f"{format_duration(time.monotonic() - stage_started_at)}."
        )
    else:
        log("Playlist is already empty.")
except Exception as e:
    print(f"[ERROR] Failed to clear existing playlist items: {e}", file=sys.stderr)
    sys.exit(6)

# ---------------------------
# Fetch episodes & add in chunks
# ---------------------------
if not episode_order:
    print("[INFO] No episodes to add. Leaving playlist empty.")
    sys.exit(0)

items_to_add = []
failed_fetch = 0
fetch_started_at = time.monotonic()
total_episodes = len(episode_order)
progress_interval = 25
log(f"Fetching {total_episodes} episodes from Plex; progress reports every {progress_interval} items.")
for index, rk in enumerate(episode_order, start=1):
    item_started_at = time.monotonic()
    try:
        items_to_add.append(plex.fetchItem(rk))
    except Exception as e:
        failed_fetch += 1
        print(f"[WARN] Could not fetch episode ratingKey={rk}: {e}", file=sys.stderr)

    item_elapsed = time.monotonic() - item_started_at
    if item_elapsed >= 5:
        log(
            f"Slow episode fetch: ratingKey={rk} took {format_duration(item_elapsed)}.",
            "WARN",
        )

    if index % progress_interval == 0 or index == total_episodes:
        fetch_elapsed = time.monotonic() - fetch_started_at
        rate = index / fetch_elapsed if fetch_elapsed else 0
        remaining_seconds = (total_episodes - index) / rate if rate else 0
        log(
            f"Fetched {index}/{total_episodes} episodes ({index / total_episodes:.0%}); "
            f"{rate:.1f} items/s; ETA {format_duration(remaining_seconds)}; "
            f"failures: {failed_fetch}."
        )

log(
    f"Episode fetch complete in {format_duration(time.monotonic() - fetch_started_at)}: "
    f"{len(items_to_add)} fetched, {failed_fetch} failed."
)

added_total = 0
try:
    add_started_at = time.monotonic()
    batches = list(chunked(items_to_add, 500))
    log(f"Adding episodes to the playlist in {len(batches)} batch(es).")
    for batch_number, batch in enumerate(batches, start=1):
        if not batch:
            continue
        batch_started_at = time.monotonic()
        playlist.addItems(batch)
        added_total += len(batch)
        log(
            f"Added batch {batch_number}/{len(batches)} ({len(batch)} items) in "
            f"{format_duration(time.monotonic() - batch_started_at)}; "
            f"running total: {added_total}/{len(items_to_add)}."
        )
except Exception as e:
    print(f"[ERROR] Failed while adding items to playlist '{playlist.title}': {e}", file=sys.stderr)
    sys.exit(7)

log(
    f"Added {added_total} episodes to playlist '{playlist.title}' in "
    f"{format_duration(time.monotonic() - add_started_at)}. "
    f"Total generation time: {format_duration(time.monotonic() - started_at)}.",
    "SUCCESS",
)
sys.exit(0)
