#!/usr/bin/env python3
"""
listUsers.py

Usage:
  python listUsers.py

Purpose:
  Lists the Plex Home users whose watched history can be used when building the
  playlist, and prints JSON:
    {"ok": true, "users": [{"user": "Kids", "title": "Kids", "owner": false}, ...]}

Notes:
  - Only the server owner can read other users' watched state, and only Plex Home
    (managed/shared-home) users can be switched to.
  - `user` is the value to hand to PlexServer.switchUser(). Managed users have no
    username or email, so plexapi can only match them by title.

Environment:
  - .env in project root with:
      PLEX_URL
      PLEX_TOKEN
      PLEX_VERIFY_SSL (optional; default "false")

Exit codes:
  2 -> .env missing or PLEX_* missing
  3 -> Plex connection failed
  4 -> Could not read the Plex account / user list
  0 -> Success
"""

import os
import sys
import json
from urllib.parse import urlparse, urlunparse

import requests
from dotenv import load_dotenv
from plexapi.server import PlexServer

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
ENV_PATH = os.path.join(ROOT, '.env')


def jerr(msg: str, code: int) -> None:
    print(json.dumps({"ok": False, "error": msg}))
    sys.exit(code)


if not os.path.exists(ENV_PATH):
    jerr(f".env not found at {ENV_PATH}", 2)

load_dotenv(ENV_PATH, override=True)

PLEX_URL = os.getenv('PLEX_URL', '').strip()
PLEX_TOKEN = os.getenv('PLEX_TOKEN', '').strip()
PLEX_VERIFY_SSL = os.getenv('PLEX_VERIFY_SSL', 'false').strip().lower() in ('1', 'true', 'yes')

if not PLEX_URL or not PLEX_TOKEN:
    jerr("Missing PLEX_URL or PLEX_TOKEN", 2)


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


PLEX_URL = remap_localhost_for_container(PLEX_URL)

try:
    session = requests.Session()
    session.verify = True if PLEX_VERIFY_SSL else False
    plex = PlexServer(PLEX_URL, PLEX_TOKEN, session=session)
except Exception as e:
    jerr(f"Plex connect failed: {e}", 3)

try:
    account = plex.myPlexAccount()
except Exception as e:
    jerr(f"Could not read the Plex account (owner token required): {e}", 4)

users = [{
    "user": "",
    "title": (getattr(account, 'title', '') or getattr(account, 'username', '') or 'Owner'),
    "owner": True,
}]

try:
    for u in account.users():
        if not getattr(u, 'home', False):
            continue
        title = getattr(u, 'title', '') or getattr(u, 'username', '') or getattr(u, 'email', '')
        if not title:
            continue
        users.append({"user": title, "title": title, "owner": False})
except Exception as e:
    jerr(f"Could not list Plex Home users: {e}", 4)

print(json.dumps({"ok": True, "users": users}))
sys.exit(0)
