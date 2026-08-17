#!/usr/bin/env python3
"""
getEpisodes.py

Usage:
  python getEpisodes.py

Purpose:
  Reads selected shows (id, timeSlot, slotPriority) from SQLite table `playlistShows`,
  queries Plex for all episodes in those shows, and populates `playlistEpisodes`.

Environment:
  - .env in project root with:
      PLEX_URL
      PLEX_TOKEN
      PLEX_VERIFY_SSL (optional; default "false")

Exit codes:
  1 -> SQLite error / write failure
  2 -> Missing PLEX_URL or PLEX_TOKEN
  3 -> Plex connection failed
  0 -> Success
"""

import os
import sys
import math
import sqlite3
import time
from urllib.parse import urlparse, urlunparse

import requests
from dotenv import load_dotenv
from plexapi.server import PlexServer

# ---------------------------
# Paths & .env loading
# ---------------------------
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
ENV_PATH = os.path.join(ROOT, '.env')
DB_FILE = os.path.join(ROOT, 'database', 'plex_playlist.db')

if not os.path.exists(ENV_PATH):
    print(f"[ERROR] .env not found at {ENV_PATH}", file=sys.stderr)
    sys.exit(2)

load_dotenv(ENV_PATH, override=True)

PLEX_URL = os.getenv('PLEX_URL', '').strip()
PLEX_TOKEN = os.getenv('PLEX_TOKEN', '').strip()
PLEX_VERIFY_SSL = os.getenv('PLEX_VERIFY_SSL', 'false').strip().lower() in ('1', 'true', 'yes')

if not PLEX_URL or not PLEX_TOKEN:
    print("[ERROR] Missing PLEX_URL or PLEX_TOKEN in .env", file=sys.stderr)
    sys.exit(2)

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
# Connect to Plex
# ---------------------------
stage_started_at = time.monotonic()
log(f"Connecting to Plex at {PLEX_URL}.")
try:
    session = requests.Session()
    session.verify = True if PLEX_VERIFY_SSL else False
    plex = PlexServer(PLEX_URL, PLEX_TOKEN, session=session)
    log(f"Connected to Plex in {format_duration(time.monotonic() - stage_started_at)}.")
except Exception as e:
    print(f"[ERROR] Plex connect failed: {e}", file=sys.stderr)
    sys.exit(3)

# ---------------------------
# Connect to Database
# ---------------------------
try:
    db_conn = sqlite3.connect(DB_FILE)
    cursor = db_conn.cursor()
    log("Connected to SQLite DB.")
except sqlite3.Error as e:
    print(f"[ERROR] SQLite connect failed: {e}", file=sys.stderr)
    sys.exit(1)

# Clear the table before refilling
try:
    cursor.execute("DELETE FROM playlistEpisodes")
    db_conn.commit()
    log("Cleared playlistEpisodes.")
except sqlite3.Error as e:
    print(f"[ERROR] Could not clear playlistEpisodes: {e}", file=sys.stderr)
    cursor.close()
    db_conn.close()
    sys.exit(1)

# ---------------------------
# Fetch selected shows (ratingKey + timeSlot + slotPriority)
# ---------------------------
cursor.execute("SELECT id, timeSlot, COALESCE(slotPriority, 1) FROM playlistShows")
rows = cursor.fetchall()
shows_from_db = {int(rk): (ts, int(prio)) for rk, ts, prio in rows}
log(f"Selected shows: {len(shows_from_db)}.")

# ---------------------------
# Gather TV libraries (type == 'show')
# ---------------------------
stage_started_at = time.monotonic()
log("Reading Plex library sections.")
tv_sections = [s for s in plex.library.sections() if getattr(s, 'type', '') == 'show']
log(
    f"Found {len(tv_sections)} TV libraries in "
    f"{format_duration(time.monotonic() - stage_started_at)}."
)
if not tv_sections:
    print("[WARN] No TV Show libraries found.")

matched_shows = 0
total_episodes_processed = 0

for section_number, section in enumerate(tv_sections, start=1):
    section_started_at = time.monotonic()
    log(f"Loading TV library {section_number}/{len(tv_sections)}: '{section.title}'.")
    library_shows = section.all()
    log(
        f"Loaded {len(library_shows)} shows from '{section.title}' in "
        f"{format_duration(time.monotonic() - section_started_at)}."
    )
    for show in library_shows:
        try:
            rk = int(show.ratingKey)
        except Exception:
            continue

        if rk not in shows_from_db:
            continue

        matched_shows += 1
        slot, slot_priority = shows_from_db[rk]
        show_started_at = time.monotonic()
        show_title = getattr(show, 'title', f'ratingKey={rk}')
        log(f"Fetching episodes for selected show {matched_shows}/{len(shows_from_db)}: '{show_title}'.")
        episodes = show.episodes()
        show_episode_count = 0
        log(f"Writing {len(episodes)} episodes for '{show_title}' to the database.")

        for ep in episodes:
            try:
                # Duration is stored (rounded up) in minutes
                duration_ms = getattr(ep, 'duration', 0) or 0
                duration_minutes = math.ceil(duration_ms / 60000) if duration_ms else 0

                insert_stmt = ("""
                    INSERT INTO playlistEpisodes
                    (ratingKey, season, episode, releaseDate, duration, summary,
                     watchedStatus, title, episodeTitle, show_id, timeSlot, slotPriority)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """)
                data = (
                    int(ep.ratingKey),
                    getattr(ep, 'parentIndex', None),
                    getattr(ep, 'index', None),
                    getattr(ep, 'originallyAvailableAt', None),
                    duration_minutes,
                    getattr(ep, 'summary', None),
                    bool(getattr(ep, 'viewCount', 0)),
                    getattr(ep, 'grandparentTitle', '') or '',
                    getattr(ep, 'title', '') or '',
                    rk,
                    slot,
                    slot_priority
                )
                cursor.execute(insert_stmt, data)
                db_conn.commit()
                total_episodes_processed += 1
                show_episode_count += 1
                if show_episode_count % 100 == 0:
                    log(
                        f"Wrote {show_episode_count}/{len(episodes)} episodes for "
                        f"'{show_title}'."
                    )
            except sqlite3.Error as e:
                print(f"[WARN] Insert failed for episode {getattr(ep, 'title', '<unknown>')}: {e}", file=sys.stderr)

        log(
            f"Completed '{show_title}': {show_episode_count}/{len(episodes)} episodes written in "
            f"{format_duration(time.monotonic() - show_started_at)}."
        )

log(
    f"DB update complete. {matched_shows} shows matched and "
    f"{total_episodes_processed} episodes processed in "
    f"{format_duration(time.monotonic() - started_at)}.",
    "SUCCESS",
)

cursor.close()
db_conn.close()
sys.exit(0)
