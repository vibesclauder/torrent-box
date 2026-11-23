#!/usr/bin/env python3
"""torrent-box dashboard: the box's start page, with links to its web apps and their health.

Served on port 80 by torrent-box-dashboard.service (installed by setup.sh), so the box's name
alone (http://<box>/) opens it, on the LAN and through Tailscale. Links use the name the page
was opened with. Reads the same config.ini/secrets.ini as rss2transmission and the apps'
API keys, and never shows a secret. GET /status.json returns the same data as JSON.
With Prowlarr, POST /ncore-code takes a fresh nCore 2FA code (the page shows a form for it
while one is needed).
It also watches: whatever stays wrong for ALERT_AFTER is pushed to the ntfy topic, and
pushed again once it's fine. And every UPDATE_EVERY it asks GitHub whether torrent-box has
moved on since the commit it was installed from: a banner on the page, and one push.

Standard library only; one round of checks serves every request for a few seconds.
"""

import argparse
import base64
import calendar
import collections
import configparser
import errno
import functools
import hashlib
import hmac
import html
import http.client
import ipaddress
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = ""  # "1.0 rev 2": setup.sh writes it in when installing
# What setup.sh installed from: {"commit", "date", "repo"} (read when needed, so a new commit
# alone doesn't change this program and restart it). No commit: no update checks.
INSTALLED_FILE = Path("/usr/local/share/torrent-box/installed.json")
GITHUB_REPO = "vibesclauder/torrent-box"
BRANCH = "main"
UPDATE_EVERY = 6 * 3600  # how often GitHub is asked (60 requests an hour are free)
UPDATE_RETRY = 3600  # after a failed check
UPDATE_PUSH_EVERY = 86400  # at most one update push this often, however many commits land
UPDATE_SUBJECTS = 5  # commit subjects shown on the page and in the push
DEFAULT_CONFIG = Path("~/.config/rss2transmission/config.ini").expanduser()
DEFAULT_ARR_DIR = Path("~/.local/share/torrent-box/arr").expanduser()
CACHE_SECONDS = 5
TIMEOUT = 3  # per check: a hung app shouldn't hold up the page
REFRESH_SECONDS = 20  # the page reloads its contents this often
STALE_RUN = 30 * 60  # rss2transmission runs every 5 minutes; this long without one is wrong
LOGIN_TIMEOUT = 60  # Prowlarr logs in to nCore while saving the indexer
LOGIN_TRIES = 5  # 2FA logins per LOGIN_WINDOW, so nobody on the LAN can hammer nCore's login
LOGIN_WINDOW = 15 * 60
SHOW_LOGIN = 10 * 60  # how long the page shows how the last code went
# After a new code, what the apps wanted while nCore was out is searched again: from a while
# before the first failure (the session ended a bit earlier), or the last RESEARCH_DEFAULT
# when no app remembers one, never further back than RESEARCH_LONGEST; the newest
# RESEARCH_MAX per app, since nCore limits requests.
RESEARCH_MARGIN = 3600
RESEARCH_DEFAULT = 3 * 86400
RESEARCH_LONGEST = 14 * 86400
RESEARCH_MAX = 30
SYNC_WAIT = 60  # for Prowlarr to send the indexer to the apps again
CODE = re.compile(r"\d{6,8}")
WATCH_EVERY = 60
ALERT_AFTER = 10 * 60  # a restart or a slow start shouldn't push
# From which state a check is pushed (default: WARN). Some push on their own already:
# rss2transmission warns about the storage disk, Prowlarr about its health (nCore too), and
# smartd about the disk's; for those only "down" (they can't tell), or nothing.
ALERT_FROM = {"storage": None, "ncore": None, "prowlarr": "down", "disk": "down",
              "ntfy": None}  # pushes about our ntfy server would go through it
NTFY_PRIORITIES = {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5}
SMART_DIR = Path("/var/lib/smartmontools")  # smartd's attribute logs, every 30 min
SMART_BAD = [(5, "reallocated"), (197, "pending"), (198, "uncorrectable")]  # sectors
DISK_HOT = 55  # °C; smartd pushes at this too (smartd.conf -W)

OK, WARN, DOWN = "ok", "warn", "down"
WORST = {OK: 0, WARN: 1, DOWN: 2}

# The apps' tiles: (check id, name, what it's for, port). Shown when the check exists. On a
# page opened over HTTPS (setup.sh's [setup] tailscale_https), they link to the apps' HTTPS
# ports on the tailnet: their own plus HTTPS_OFFSET.
HTTPS_OFFSET = 10000
TILES = [
    ("jellyfin", "Jellyfin", "Movies and series", 8096),
    ("minidlna", "MiniDLNA", "DLNA server", 8200),
    ("seerr", "Seerr", "Movies and series", 5055),
    ("desireerr", "Desireerr", "Adult content", 5056),
    ("transmission", "Transmission", "nCore torrents", 9091),
    ("sonarr", "Sonarr", "Series", 8989),
    ("radarr", "Radarr", "Movies", 7878),
    ("whisparr", "Whisparr", "Adult", 6969),
    ("prowlarr", "Prowlarr", "nCore search", 9696),
]
# The tiles in groups (the group says what for, the tile what's in it). The last one is the
# apps setup.sh configures and nobody needs day to day: folded away on the page and in the bar.
GROUPS = [
    ("Watch", ("jellyfin", "minidlna")),
    ("Request", ("seerr", "desireerr")),
    ("Download", ("transmission",)),
    ("Monitor & grab", ("sonarr", "radarr", "whisparr", "prowlarr")),
]
FOLDED = GROUPS[-1][1]
# The health rows: (check id, name)
ROWS = [
    ("storage", "Storage"),
    ("disk", "Disk health"),
    ("rss2transmission", "nCore feed"),
    ("ncore", "nCore search"),
    ("samba", "Samba share"),
    ("ntfy", "Notifications"),
    ("tailscale", "Tailscale"),
    ("system", "System"),
]
ARR_APPS = {"prowlarr": (9696, "v1"), "sonarr": (8989, "v3"), "radarr": (7878, "v3"),
            "whisparr": (6969, "v3")}  # Whisparr only with [setup] whisparr = yes
# The apps' own logos, for their tiles: files they install, or URLs they serve (the Docker
# ones). The first that exists wins; none -> a glyph of ours. Never copied into this repo.
LOGOS = {
    "jellyfin": ["/usr/share/jellyfin/web/icon-transparent.*.png",
                 "/usr/share/jellyfin/web/touchicon.*.png"],
    "transmission": ["/usr/share/icons/hicolor/scalable/apps/transmission-gtk.svg",
                     "/usr/share/transmission/public_html/images/webclip-icon.png"],
    "seerr": ["http://127.0.0.1:5055/os_icon.svg"],
    "sonarr": ["http://127.0.0.1:8989/Content/Images/logo.svg"],
    "radarr": ["http://127.0.0.1:7878/Content/Images/logo.svg"],
    "prowlarr": ["http://127.0.0.1:9696/Content/Images/logo.svg"],
    "whisparr": ["http://127.0.0.1:6969/Content/Images/logo.svg"],
    "desireerr": ["http://127.0.0.1:5056/static/logo.svg"],
}
LOGO_TYPES = {".svg": "image/svg+xml", ".png": "image/png"}
LOGO_MAX = 512 * 1024
LOGO_RETRY = 60  # seconds before trying again to get a logo that wasn't there
# The box's other web services, found by themselves: whatever listens on TCP and answers with
# an HTML page, apart from the tiles' apps, ntfy and this page. Listed under "Other services".
OTHERS_EVERY = 60  # seconds between looks
OTHERS_WAIT = 4  # the first look may hold up the page this long; later ones never do
OTHERS_TIMEOUT = 2  # per port
OTHERS_READ = 64 * 1024  # of a page, enough for its <title>
NOT_WEB = {22, 25, 53, 111, 139, 445, 631}  # well-known ports that are never a web page
DOCKER_NET = ipaddress.ip_network("172.16.0.0/12")
# tailscaled's own listeners (it serves the apps' HTTPS ports there): not services of their own
TAILNET_NETS = [ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48")]


### Settings

def load_settings(config_path):
    """What the page needs from config.ini and secrets.ini; defaults for anything missing."""
    def read(path):
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(path, encoding="utf-8")
        except (configparser.Error, UnicodeDecodeError):
            pass
        return parser

    conf = read(config_path)
    secrets = read(Path(config_path).parent / "secrets.ini")

    def get(parser, section, key, fallback=""):
        return parser.get(section, key, fallback=fallback).strip()

    try:
        warn_percent = int(get(conf, "general", "disk_warn_percent", "90"))
    except ValueError:
        warn_percent = 90
    return {
        "storage_root": Path(get(conf, "general", "storage_root", "/mnt/storage")).expanduser(),
        "require_mount": get(conf, "general", "require_mount", "yes").lower()
                         in ("yes", "true", "on", "1"),
        "disk_warn_percent": warn_percent,
        "rpc_url": get(conf, "transmission", "rpc_url", "http://localhost:9091/transmission/rpc"),
        "ntfy_server": get(conf, "ntfy", "server", "https://ntfy.sh").rstrip("/"),
        "ntfy_topic": get(secrets, "secrets", "ntfy_topic"),
        "priority_problem": NTFY_PRIORITIES.get(get(conf, "ntfy", "priority_problem", "high").lower(), 4),
        "rpc_user": get(secrets, "secrets", "rpc_user"),
        "rpc_password": get(secrets, "secrets", "rpc_password"),
        "media_server": get(conf, "setup", "media_server", "none").lower() or "none",
        "samba": get(conf, "setup", "samba").lower() == "yes",
        "tailscale": get(conf, "setup", "tailscale").lower() == "yes",
        "own_ntfy": get(conf, "setup", "own_ntfy").lower() == "yes",
        "arr": get(conf, "setup", "arr").lower() == "yes",
        "whisparr": get(conf, "setup", "whisparr").lower() == "yes",
    }


### Helpers

def result(state, summary, details=(), upstream=(), **extra):
    """A check's outcome. upstream: what it reports about something else (nCore, Prowlarr)."""
    return {"state": state, "summary": summary, "details": list(details),
            "upstream": list(upstream), **extra}


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def ago(seconds):
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60:.0f} min ago"
    if seconds < 2 * 86400:
        return f"{seconds // 3600:.0f} h ago"
    return f"{seconds // 86400:.0f} days ago"


def duration(seconds):
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    if days:
        return f"{days} day{'s' if days > 1 else ''} {hours} h"
    return f"{hours} h {rest // 60} min"


def http_get(url, headers=None, timeout=TIMEOUT):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        e.close()
        raise


def problem(e):
    """A short reason from a failed HTTP call."""
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {e.code}"
    reason = getattr(e, "reason", e)
    if isinstance(reason, ConnectionRefusedError):
        return "not running"
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "not responding"
    return str(reason)


def unit_state(unit, user=False):
    """`systemctl show` properties of a unit ({} if systemd can't be asked)."""
    cmd = ["systemctl", *(["--user"] if user else []), "show", "--timestamp=unix",
           "-p", "LoadState,ActiveState,SubState,Result,ExecMainExitTimestamp", unit]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def unix_time(value):
    """systemd's "@1730000000" (--timestamp=unix) -> seconds, or None."""
    match = re.fullmatch(r"@(\d+)", value or "")
    return int(match[1]) if match else None


def service_check(unit):
    """(state, summary) of a system service."""
    st = unit_state(unit)
    active = st.get("ActiveState", "")
    if active == "active":
        return OK, "running"
    if not st:
        return WARN, "can't ask systemd"
    if st.get("LoadState") == "not-found":
        return DOWN, "not installed"
    if active in ("activating", "reloading"):
        return WARN, "starting"
    return DOWN, "failed" if active == "failed" else "stopped"


### Checks

class Transmission:
    """Just enough of Transmission's RPC: the session id dance and one call."""

    def __init__(self, url, user, password):
        self.url = url
        self.auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        self.session_id = ""
        self.version = None

    def call(self, method, arguments):
        body = json.dumps({"method": method, "arguments": arguments}).encode()
        for _ in range(2):  # a 409 hands us a fresh session id
            req = urllib.request.Request(self.url, data=body, headers={
                "Authorization": self.auth, "X-Transmission-Session-Id": self.session_id,
                "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                    return json.loads(resp.read()).get("arguments", {})
            except urllib.error.HTTPError as e:
                e.close()
                if e.code != 409:
                    raise
                self.session_id = e.headers.get("X-Transmission-Session-Id", "")
        raise OSError("no RPC session id")


_transmission = {}  # one client per login, keeps its session id between checks


def check_transmission(s):
    state, summary = service_check("transmission-daemon")
    if state != OK:
        return result(state, summary)
    key = (s["rpc_url"], s["rpc_user"], s["rpc_password"])
    tr = _transmission.setdefault(key, Transmission(*key))
    try:
        torrents = tr.call("torrent-get", {"fields": [
            "name", "status", "error", "errorString", "rateDownload", "rateUpload"]})["torrents"]
    except urllib.error.HTTPError as e:
        why = "login refused" if e.code == 401 else problem(e)
        return result(WARN, f"running, but its RPC says: {why}")
    except (OSError, ValueError, KeyError) as e:
        return result(WARN, f"running, but its RPC: {problem(e)}")
    # status: 0 stopped, 1-2 checking, 3-4 (queued to) download, 5-6 (queued to) seed
    downloading = sum(t["status"] in (3, 4) for t in torrents)
    seeding = sum(t["status"] in (5, 6) for t in torrents)
    down = sum(t["rateDownload"] for t in torrents)
    up = sum(t["rateUpload"] for t in torrents)
    try:  # for the tile; changes only with an upgrade, which restarts the daemon
        version = tr.version or tr.call("session-get", {"fields": ["version"]}).get("version")
        tr.version = version
    except (OSError, ValueError):
        version = None
    parts = [f"{downloading} downloading", f"{seeding} seeding"]
    if down or up:
        parts.append(f"↓ {human_size(down)}/s ↑ {human_size(up)}/s")
    # local errors (3) are ours to fix (disk, permissions); tracker ones (1, 2) are nCore's
    broken = [t for t in torrents if t["error"] == 3]
    details = [f"{t['name']}: {t['errorString']}" for t in broken[:5]]
    if len(broken) > 5:
        details.append(f"… and {len(broken) - 5} more")
    tracker = sum(t["error"] in (1, 2) for t in torrents)
    upstream = [f"{tracker} with a tracker warning"] if tracker else []
    return result(WARN if broken else OK, ", ".join(parts), details, upstream,
                  version=version, rate_down=down, rate_up=up)


def check_jellyfin(s):
    state, summary = service_check("jellyfin")
    if state != OK:
        return result(state, summary)
    try:
        health = http_get("http://127.0.0.1:8096/health").decode().strip()
    except OSError as e:
        return result(WARN, f"running, but {problem(e)}")
    try:
        version = json.loads(http_get("http://127.0.0.1:8096/System/Info/Public")).get("Version")
    except (OSError, ValueError, AttributeError):
        version = None
    return result(OK if health == "Healthy" else WARN, health.lower() or "no answer", version=version)


def check_minidlna(s):
    return result(*service_check("minidlna"))


def check_samba(s):
    return result(*service_check("smbd"))


def check_ntfy(s):
    """Our own ntfy server ([setup] own_ntfy = yes): the service and its health endpoint."""
    state, summary = service_check("ntfy")
    server = s["ntfy_server"]
    if state != OK:
        return result(state, summary, server=server)
    try:
        healthy = json.loads(http_get(f"{server}/v1/health")).get("healthy")
    except (OSError, ValueError) as e:
        return result(WARN, f"running, but {problem(e) if isinstance(e, OSError) else 'no health'}",
                      server=server)
    return result(OK if healthy else WARN, "own server, running" if healthy else "unhealthy",
                  server=server)


def ntfy_link(server, host, https=False):
    """The ntfy server's address as the browser can reach it: "localhost" in the config is
    this box, which the viewer knows as host (over HTTPS: on its HTTPS port, see TILES)."""
    parts = urllib.parse.urlsplit(server)
    if parts.hostname not in ("localhost", "127.0.0.1", "::1"):
        return server
    if https:
        return f"https://{host}:{(parts.port or 80) + HTTPS_OFFSET}"
    return parts._replace(netloc=f"{host}:{parts.port}" if parts.port else host).geturl()


def api_key(config_xml):
    try:
        return ET.parse(config_xml).getroot().findtext("ApiKey") or None
    except (OSError, ET.ParseError):
        return None


def check_arr(name, arr_dir):
    port, api = ARR_APPS[name]
    key = api_key(Path(arr_dir) / name / "config.xml")
    try:
        if not key:  # can't read its health without it; is it up at all?
            http_get(f"http://127.0.0.1:{port}/ping")
            return result(OK, "running")
        issues = json.loads(http_get(f"http://127.0.0.1:{port}/api/{api}/health",
                                     {"X-Api-Key": key}))
    except (OSError, ValueError) as e:
        return result(DOWN, problem(e))
    issues = [i for i in issues if i.get("type") in ("warning", "error")]
    elsewhere = [i.get("message", "?") for i in issues if upstream_issue(name, i)]
    issues = [i.get("message", "?") for i in issues if not upstream_issue(name, i)]
    try:
        version = json.loads(http_get(f"http://127.0.0.1:{port}/api/{api}/system/status",
                                      {"X-Api-Key": key})).get("version")
    except (OSError, ValueError, AttributeError):
        version = None
    if not issues:
        return result(OK, "healthy", upstream=elsewhere, version=version)
    return result(WARN, f"{len(issues)} issue{'s' if len(issues) > 1 else ''}", issues, elsewhere,
                  version=version)


# Health checks of Sonarr, Radarr and Whisparr that report on something else: their
# indexer is Prowlarr (nCore behind it, which has its own tile and pushes), and an import
# list is an outside site (StashDB). nCore's request limit alone used to push "Sonarr needs
# a look" for a minute's backoff. Prowlarr's own indexer issues stay its own.
UPSTREAM_HEALTH = {"IndexerStatusCheck", "IndexerLongTermStatusCheck", "ImportListStatusCheck"}


def upstream_issue(app, issue):
    if app == "prowlarr":
        return False
    source = issue.get("source", "")
    return source in UPSTREAM_HEALTH or (
        source in ("IndexerRssCheck", "IndexerSearchCheck")
        and "temporarily unavailable" in issue.get("message", ""))


class LoginError(Exception):
    pass


def prowlarr_call(arr_dir, method, path, body=None, timeout=TIMEOUT):
    """Prowlarr's API. Raises LoginError with Prowlarr's own reason when it says no."""
    key = api_key(Path(arr_dir) / "prowlarr" / "config.xml")
    if not key:
        raise LoginError("Prowlarr has no API key yet")
    port, version = ARR_APPS["prowlarr"]
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/{version}/{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"X-Api-Key": key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode()
    except urllib.error.HTTPError as e:
        with e:
            reason = prowlarr_reason(e.read().decode(errors="replace"))
        raise LoginError(reason or f"HTTP {e.code}") from None
    except OSError as e:
        raise LoginError(f"Prowlarr: {problem(e)}") from None
    return json.loads(text) if text.strip() else None


def prowlarr_reason(body):
    """The messages in Prowlarr's error reply (a list of validation failures, or one)."""
    try:
        reply = json.loads(body)
    except ValueError:
        return body.strip()[:300]
    failures = reply if isinstance(reply, list) else [reply]
    return "; ".join(str(f.get("errorMessage") or f.get("message") or "")
                     for f in failures if isinstance(f, dict)).strip("; ")[:300]


def find_ncore(arr_dir):
    return next((i for i in prowlarr_call(arr_dir, "GET", "indexer")
                 if i.get("definitionName") == "ncore"), None)


def ncore_login(arr_dir, code):
    """Switch Prowlarr's nCore indexer on with this 2FA code: saving it makes Prowlarr log in
    (and keep the session); then it's sent to Sonarr and Radarr. The same as setup_helper.py
    ncore-code (this program is installed on its own, so it can't import that)."""
    indexer = find_ncore(arr_dir)
    if indexer is None:
        raise LoginError("Prowlarr has no nCore indexer (run setup.sh)")
    fields = {f["name"]: f for f in indexer.get("fields", [])}
    if "2facode" not in fields:
        raise LoginError("nCore's indexer has no 2FA field")
    indexer["enable"] = True
    fields["2facode"]["value"] = code
    prowlarr_call(arr_dir, "PUT", f"indexer/{indexer['id']}", indexer, LOGIN_TIMEOUT)
    return prowlarr_call(arr_dir, "POST", "command",
                         {"name": "ApplicationIndexerSync", "forceSync": True})


# Sonarr, Radarr and Whisparr search for something once, when it's added (or for an episode,
# by RSS when it's uploaded). When nCore was logged out then, that search failed and nothing
# tries again: a request made meanwhile just sat there. So after a new code, what they wanted
# while nCore was out gets searched again.

def arr_call(arr_dir, app, method, path, body=None, timeout=LOGIN_TIMEOUT):
    """An app's API; OSError (or ValueError for a reply that isn't JSON) when it fails."""
    key = api_key(Path(arr_dir) / app / "config.xml")
    if not key:
        raise OSError(f"{app} has no API key")
    port, version = ARR_APPS[app]
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/{version}/{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"X-Api-Key": key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        text = resp.read().decode()
    return json.loads(text) if text.strip() else None


def outage_start(arr_dir, now=None):
    """Since when the apps wanted things nCore couldn't give: from Prowlarr's first failure
    (read before logging in, which clears it)."""
    now = now or time.time()
    try:
        firsts = [iso_time(st.get("initialFailure"))
                  for st in arr_call(arr_dir, "prowlarr", "GET", "indexerstatus", timeout=TIMEOUT)]
    except (OSError, ValueError):
        firsts = []
    firsts = [t for t in firsts if t]
    since = min(firsts) - RESEARCH_MARGIN if firsts else now - RESEARCH_DEFAULT
    return max(since, now - RESEARCH_LONGEST)


def wait_for_command(arr_dir, app, command, seconds=SYNC_WAIT):
    deadline = time.monotonic() + seconds
    while command and command.get("id") and time.monotonic() < deadline:
        try:
            command = arr_call(arr_dir, app, "GET", f"command/{command['id']}")
        except (OSError, ValueError):
            return
        if command.get("status") in ("completed", "failed", "aborted", "cancelled", "orphaned"):
            return
        time.sleep(1)


def revive_indexers(arr_dir, app):
    """After failures an app leaves an indexer out for a while, longer each time (up to a
    day); a successful test ends that, so the search right after the login uses nCore. Which
    one is left out only shows in its health checks."""
    if not any(i.get("source", "").startswith("Indexer")
               for i in arr_call(arr_dir, app, "GET", "health")):
        return
    for indexer in arr_call(arr_dir, app, "GET", "indexer"):
        if indexer.get("enableRss") or indexer.get("enableAutomaticSearch"):
            try:
                arr_call(arr_dir, app, "POST", "indexer/test", indexer)
            except (OSError, ValueError):
                pass  # then the search finds no indexer, as it would have anyway


def wanted_since(item, since, now, keys):
    return any(t and since <= t <= now for t in (iso_time(item.get(k)) for k in keys))


def research_movies(arr_dir, app, since, now):
    """Radarr's movies (Whisparr's scenes) without a file, added or released since."""
    movies = [m for m in arr_call(arr_dir, app, "GET", "movie")
              if m.get("monitored") and not m.get("hasFile") and m.get("isAvailable", True)
              and wanted_since(m, since, now, ("added", "inCinemas", "digitalRelease",
                                               "physicalRelease", "releaseDate"))]
    movies.sort(key=lambda m: iso_time(m.get("added")) or 0, reverse=True)
    ids = [m["id"] for m in movies[:RESEARCH_MAX]]
    if ids:
        arr_call(arr_dir, app, "POST", "command", {"name": "MoviesSearch", "movieIds": ids})
    noun = "scene" if app == "whisparr" else "movie"
    return [f"{len(ids)} {noun}{'s' if len(ids) > 1 else ''}"] if ids else []


def research_series(arr_dir, app, since, now):
    """Sonarr's missing episodes: whole series added since (season packs first), and the
    episodes of others that aired since."""
    records = arr_call(arr_dir, app, "GET", "wanted/missing?page=1&pageSize=1000&monitored=true"
                       "&includeSeries=true&sortKey=airDateUtc&sortDirection=descending")["records"]
    series = list(dict.fromkeys(r["seriesId"] for r in records
                                if wanted_since(r.get("series") or {}, since, now, ("added",))))
    series = series[:RESEARCH_MAX]
    episodes = [r["id"] for r in records if r["seriesId"] not in series
                and wanted_since(r, since, now, ("airDateUtc",))][:RESEARCH_MAX]
    for sid in series:
        arr_call(arr_dir, app, "POST", "command", {"name": "SeriesSearch", "seriesId": sid})
    if episodes:
        arr_call(arr_dir, app, "POST", "command", {"name": "EpisodeSearch", "episodeIds": episodes})
    return ([f"{len(series)} series"] if series else []) + \
        ([f"{len(episodes)} episode{'s' if len(episodes) > 1 else ''}"] if episodes else [])


def research(arr_dir, since, sync=None, now=None):
    """Search again for what the apps wanted since `since`, once Prowlarr's `sync` command
    has sent them the indexer. Returns what was searched ("1 movie", …) and what failed."""
    wait_for_command(arr_dir, "prowlarr", sync)
    now = now or time.time()
    done = []
    for app, search in (("sonarr", research_series), ("radarr", research_movies),
                        ("whisparr", research_movies)):
        if not api_key(Path(arr_dir) / app / "config.xml"):
            continue  # not set up
        try:
            revive_indexers(arr_dir, app)
            done += search(arr_dir, app, since, now)
        except (OSError, ValueError, KeyError) as e:
            done.append(f"{app.capitalize()} failed: {problem(e) if isinstance(e, OSError) else e}")
    return done


def iso_time(value):
    """Prowlarr's "2026-10-29T15:42:03.1234567Z" -> seconds, or None."""
    match = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)", value or "")
    if not match:
        return None
    return calendar.timegm(time.strptime(match[1], "%Y-%m-%dT%H:%M:%S"))


def check_ncore(arr_dir, last_login=None, now=None):
    """Is Prowlarr logged in to nCore? needs_code: show the 2FA form."""
    now = now or time.time()
    details = []
    if last_login and now - last_login["time"] < SHOW_LOGIN:
        at = ago(now - last_login["time"])
        details.append(f"logged in with the new code {at}" if last_login["ok"]
                       else f"the code didn't work ({at}): {last_login['message']}")
        if "searched" in last_login:
            searched = last_login["searched"]
            details.append("searching again for what was requested meanwhile…" if searched is None
                           else "searched again: " + ", ".join(searched) if searched
                           else "nothing was waiting for nCore")
    try:
        indexer = find_ncore(arr_dir)
        if indexer is None:
            return result(WARN, "not in Prowlarr: run setup.sh", details)
        if not indexer.get("enable"):
            return result(WARN, "switched off: enter a 2FA code to log in", details, needs_code=True)
        status = next((st for st in prowlarr_call(arr_dir, "GET", "indexerstatus")
                       if st.get("indexerId") == indexer["id"]), None)
    except LoginError as e:
        return result(DOWN, str(e), details)
    failed = iso_time((status or {}).get("mostRecentFailure"))
    until = iso_time((status or {}).get("disabledTill"))
    # a login with a new code since the last failure counts, even if Prowlarr's backoff
    # hasn't run out yet
    newer = last_login and last_login["ok"] and failed and last_login["time"] > failed
    if until and until > now and not newer:
        return result(WARN, f"failing since {ago(now - failed) if failed else '?'}: "
                            "the session may have ended, enter a new 2FA code",
                      details, needs_code=True)
    return result(OK, "logged in", details, needs_code=False)


SEERR_VERSION_TIMEOUT = 1
seerr_version = None  # the last one /status told us


def check_seerr(s):
    """Up is /status/appdata (local only). /status, which has the version, waits on
    GitHub's API for its update check: when GitHub hangs, that's no outage of Seerr's."""
    global seerr_version
    try:
        appdata = json.loads(http_get("http://127.0.0.1:5055/api/v1/status/appdata"))
    except (OSError, ValueError) as e:
        return result(DOWN, problem(e))
    if appdata.get("appDataPermissions") is False:
        return result(WARN, "can't write its config folder",
                      [f"{appdata.get('appDataPath', '/app/config')} in the container"])
    try:
        status = json.loads(http_get("http://127.0.0.1:5055/api/v1/status",
                                     timeout=SEERR_VERSION_TIMEOUT))
    except (OSError, ValueError):
        return result(OK, f"running, version {seerr_version or '?'}",
                      ["its update check isn't getting an answer from GitHub; "
                       "requests and everything else work"], version=seerr_version)
    seerr_version = status.get("version", "?")
    return result(OK, f"running, version {seerr_version}", version=seerr_version)


def desireerr_build(arr_dir):
    """What runs as Desireerr, when it isn't its newest release: "edge" (its dev branch),
    "local 26c5293+" (a build of a checkout), "dev" (a checkout's code), else "". From the .env
    beside the apps' compose.yml (setup_helper.py desireerr)."""
    try:
        lines = (Path(arr_dir) / ".env").read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    env = dict(line.split("=", 1) for line in lines if "=" in line)
    if env.get("DESIREERR_CODE", "").strip():
        return "dev"
    build = env.get("DESIREERR_BUILD", "").strip()
    if build.startswith("local:"):
        return "local " + build[6:]
    return "edge" if env.get("DESIREERR_IMAGE", "").strip().endswith(":dev") else ""


def check_desireerr(s, arr_dir=DEFAULT_ARR_DIR):
    """Its own status; and whether a checkout runs instead of the GitHub build (build=...)."""
    build = desireerr_build(arr_dir)
    note = [f"{build} build: ./setup_helper.py desireerr release goes back"] if build else []
    try:
        status = json.loads(http_get("http://127.0.0.1:5056/api/status"))
    except (OSError, ValueError) as e:
        return result(DOWN, problem(e), note, build=build)
    if status.get("setup_needed"):
        return result(WARN, "waiting for its first sign-in",
                      [f"the setup code is in {Path(arr_dir) / 'desireerr/data/setup-code'}"] + note,
                      build=build)
    version = status.get("version", "?")
    return result(OK, f"running, version {version}" + (f" ({build} build)" if build else ""),
                  version=status.get("version"), build=build)


def check_rss2transmission(s, now=None):
    now = now or time.time()
    timer = unit_state("rss2transmission.timer", user=True)
    if not timer:
        return result(WARN, "can't ask systemd --user")
    if timer.get("ActiveState") != "active":
        return result(DOWN, "timer not running",
                      ["systemctl --user enable --now rss2transmission.timer"])
    service = unit_state("rss2transmission.service", user=True)
    if service.get("ActiveState") == "activating":
        return result(OK, "checking the feed now")
    last = unix_time(service.get("ExecMainExitTimestamp"))
    if last is None:
        return result(OK, "waiting for its first run")
    if service.get("Result") not in ("success", None):
        return result(WARN, f"last run had errors ({ago(now - last)})",
                      ["see journalctl --user -u rss2transmission"])
    if now - last > STALE_RUN:
        return result(WARN, f"last run {ago(now - last)}")
    return result(OK, f"checks the feed every 5 min, last {ago(now - last)}")


def check_tailscale(s):
    try:
        out = subprocess.run(["tailscale", "status", "--json"], capture_output=True,
                             text=True, timeout=TIMEOUT).stdout
    except FileNotFoundError:
        return result(DOWN, "not installed")
    except (OSError, subprocess.SubprocessError) as e:
        return result(WARN, str(e))
    try:
        status = json.loads(out)
    except ValueError:
        return result(DOWN, "tailscaled not running")
    me = status.get("Self") or {}
    name = (me.get("DNSName") or "").rstrip(".")
    ipv4 = next((ip for ip in me.get("TailscaleIPs") or [] if "." in ip), "")
    state = status.get("BackendState") or "?"
    if state != "Running":
        return result(WARN, state, ["sudo tailscale up"] if state == "NeedsLogin" else [])
    return result(OK, f"connected as {name} ({ipv4})" if name else "connected", name=name,
                  certs=bool(status.get("CertDomains")), tags=list(me.get("Tags") or []),
                  **tailscale_services(me))


def tailscale_services(me):
    """The box's Tailscale Services (its apps' own names, see SERVICES): {"approved": names the
    tailnet lets it host, "served": {name: its own name on the tailnet} for those it serves on
    HTTPS 443, "links": {port: own name} for the approved ones, by the app's port (a string,
    as in status.json)}."""
    approved = set()
    for entry in (me.get("CapMap") or {}).get("service-host") or []:
        if isinstance(entry, dict):
            approved |= {svc.removeprefix("svc:") for svc in entry}
    served, links = {}, {}
    try:
        serving = json.loads(subprocess.run(["tailscale", "serve", "status", "--json"],
                                            capture_output=True, text=True, timeout=TIMEOUT).stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        serving = {}
    for svc, conf in ((serving or {}).get("Services") or {}).items():
        name = svc.removeprefix("svc:")
        for hostport, web in ((conf or {}).get("Web") or {}).items():
            host, _, port = hostport.rpartition(":")
            target = ((web or {}).get("Handlers") or {}).get("/", {}).get("Proxy") or ""
            m = re.fullmatch(r"http://(?:127\.0\.0\.1|localhost):(\d+)/?", target)
            if m and port == "443":
                served[name] = host
                if name in approved:
                    links[m[1]] = host
    return {"approved": sorted(approved), "served": served, "links": links}


### Other web services

def listening(proc=Path("/proc/net")):
    """The box's listening TCP sockets: {(address, port)}, from /proc (no root needed)."""
    found = set()
    for name in ("tcp", "tcp6"):
        try:
            lines = (proc / name).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 4 or fields[3] != "0A":  # LISTEN
                continue
            addr, _, port = fields[1].partition(":")
            raw = bytes.fromhex(addr)
            # the kernel prints each 32-bit word in host (little-endian) order
            raw = b"".join(raw[i:i + 4][::-1] for i in range(0, len(raw), 4))
            found.add((str(ipaddress.ip_address(raw)), int(port, 16)))
    return found


def tailscale_https():
    """What `tailscale serve` gives an HTTPS address: {local port: [https URLs]}, the apps'
    own names (Tailscale Services) first."""
    try:
        serving = json.loads(subprocess.run(["tailscale", "serve", "status", "--json"],
                                            capture_output=True, text=True, timeout=TIMEOUT).stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}
    webs = [((conf or {}).get("Web") or {}) for conf in ((serving or {}).get("Services") or {}).values()]
    webs.append((serving or {}).get("Web") or {})
    urls = {}
    for web in webs:
        for hostport, conf in web.items():
            host, _, port = hostport.rpartition(":")
            target = ((conf or {}).get("Handlers") or {}).get("/", {}).get("Proxy") or ""
            m = re.fullmatch(r"http://(?:127\.0\.0\.1|localhost):(\d+)/?", target)
            if m:
                urls.setdefault(int(m[1]), []).append(
                    f"https://{host}/" if port == "443" else f"https://{host}:{port}/")
    return urls


TITLE = re.compile(rb"<title[^>]*>(.*?)</title", re.I | re.S)


class IconLinks(HTMLParser):
    """A page's icons (<link rel="icon"> and the like), best first for a small badge: SVG, then
    the biggest picture. Only those on the same server (a path, not another site's URL)."""

    def __init__(self):
        super().__init__()
        self.found = []  # (rank, path)

    def handle_starttag(self, tag, attrs):
        a = {k: v or "" for k, v in attrs}
        rel = a.get("rel", "").lower().split()
        href = a.get("href", "").strip()
        if tag != "link" or not ({"icon", "apple-touch-icon", "apple-touch-icon-precomposed"} & set(rel)) \
                or not href or urllib.parse.urlsplit(href).scheme or href.startswith("//"):
            return
        sizes = [int(n) for n in re.findall(r"(\d+)x\d+", a.get("sizes", ""))]
        svg = "svg" in a.get("type", "") or urllib.parse.urlsplit(href).path.endswith(".svg")
        big = max(sizes, default=180 if "icon" not in rel else 16)  # a touch icon is 180px
        self.found.append(((0 if svg else 1, -big), href))

    @classmethod
    def paths(cls, page, base="/"):
        parser = cls()
        try:
            parser.feed(page.decode("utf-8", "replace"))
        except Exception:  # whatever the page is like, it still has a favicon.ico, maybe
            pass
        paths = [urllib.parse.urljoin(base, href) for _, href in sorted(parser.found)]
        return list(dict.fromkeys(paths + ["/favicon.ico"]))


# What a picture is, by its first bytes (what the app says it is can be wrong)
PICTURES = [(b"\x89PNG", "image/png"), (b"\x00\x00\x01\x00", "image/x-icon"),
            (b"\xff\xd8", "image/jpeg"), (b"GIF8", "image/gif")]


def picture_type(data):
    for magic, kind in PICTURES:
        if data.startswith(magic):
            return kind
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    head = data[:4096].lower()
    if b"<svg" in head and b"<html" not in head:
        return "image/svg+xml"
    return None


def fetch_icon(address, port, paths, timeout=OTHERS_TIMEOUT):
    """The first of these icons there is at address:port: (bytes, content type), or None."""
    for path in paths:
        conn = http.client.HTTPConnection(address, port, timeout=timeout)
        try:
            conn.request("GET", path, headers={"Host": f"localhost:{port}"})
            resp = conn.getresponse()
            data = resp.read(LOGO_MAX + 1) if resp.status == 200 else b""
        except (OSError, http.client.HTTPException, ValueError):
            data = b""
        finally:
            conn.close()
        if data and len(data) <= LOGO_MAX and (kind := picture_type(data)):
            return data, kind
    return None


def web_page(address, port, timeout=OTHERS_TIMEOUT):
    """What answers at address:port, if it's a web page: (its title, a digest of the page, to
    tell one app on two ports from two apps, the paths of its icons, best first), else None.
    Follows one redirect on the same server (a login page, say). This page itself is None.
    A page that turns us away (401/403: the app behind a login or a Tailscale identity check,
    which our probe doesn't pass) has no digest: other_services matches it by its title."""
    path = "/"
    for _ in range(2):
        conn = http.client.HTTPConnection(address, port, timeout=timeout)
        try:
            conn.request("GET", path, headers={"Host": f"localhost:{port}", "Accept": "text/html"})
            resp = conn.getresponse()
            if (resp.getheader("Server") or "").startswith(Handler.server_version):
                return None
            location = resp.getheader("Location") or ""
            if resp.status in (301, 302, 303, 307, 308) and location.startswith("/") \
                    and not location.startswith("//") and path == "/":
                path = location
                continue
            if "html" not in (resp.getheader("Content-Type") or "") \
                    or resp.status >= 400 and resp.status not in (401, 403):
                return None  # an error page isn't the app
            body = resp.read(OTHERS_READ)
            gated = resp.status >= 400
        except (OSError, http.client.HTTPException, ValueError):
            return None
        finally:
            conn.close()
        m = TITLE.search(body)
        title = html.unescape(m[1].decode("utf-8", "replace")).strip() if m else ""
        return (" ".join(title.split())[:80], None if gated else hashlib.sha256(body).hexdigest()[:16],
                IconLinks.paths(body, path))
    return None


def other_services(skip=(), sockets=None, https=None, probe=web_page, icon=fetch_icon):
    """The box's web services besides ours (skip: their ports), one per app however many ports
    it has: [{"name", "ports", "lan": ports any device on the LAN reaches ({port: address}, ""
    for all of the box's addresses), "https": its addresses on the tailnet, "id": the page's
    digest, "picture": its icon (bytes, content type) or None}], by name. Only those a viewer
    can reach: a service on 127.0.0.1 alone needs `tailscale serve`."""
    sockets = listening() if sockets is None else sockets
    https = tailscale_https() if https is None else https
    binds = {}
    for address, port in sockets:
        ip = ipaddress.ip_address(address)
        if port in NOT_WEB or port in skip or ip.is_link_local \
                or any(ip in net for net in TAILNET_NETS):
            continue
        binds.setdefault(port, set()).add(address)
    targets = {}
    for port, addresses in binds.items():
        anywhere = addresses & {"0.0.0.0", "::"}
        local = [a for a in addresses if ipaddress.ip_address(a).is_loopback]
        # one address: the LAN's rather than a Docker bridge's (172.16.0.0/12)
        others = sorted(addresses - anywhere - set(local),
                        key=lambda a: (ipaddress.ip_address(a) in DOCKER_NET, a))
        lan = "" if anywhere else (others[0] if others else None)
        if lan is None and port not in https:
            continue  # only on this box
        reach = (["127.0.0.1", "::1"] if anywhere else []) + local + others
        targets[port] = (lan, reach)

    def look(port):
        for address in targets[port][1]:
            if (page := probe(address, port)) is not None:
                return port, address, page
        return port, None, None

    found = {}
    if targets:
        with ThreadPoolExecutor(max_workers=min(16, len(targets))) as pool:
            looks = [hit for hit in pool.map(look, sorted(targets)) if hit[2] is not None]
            # a port that turned us away (no digest) joins the app of the same name, else stands alone
            for port, address, page in sorted(looks, key=lambda hit: hit[2][1] is None):
                title, digest, icons = page
                if digest is None:
                    named = [d for d, entry in found.items() if title and entry["name"] == title]
                    digest = named[0] if named else hashlib.sha256(f"{title}:{port}".encode()).hexdigest()[:16]
                if digest not in found:
                    found[digest] = {"name": title or f"Port {port}", "ports": [], "lan": {},
                                     "https": [], "id": digest, "picture": icon(address, port, icons)}
                entry = found[digest]
                entry["ports"].append(port)
                if targets[port][0] is not None:
                    entry["lan"][str(port)] = targets[port][0]
                entry["https"] += https.get(port, [])
    for entry in found.values():  # a service's own name (no port) first
        entry["ports"].sort()
        entry["https"].sort(key=lambda url: (url.count(":") > 1, url))
    return sorted(found.values(), key=lambda e: (e["name"].casefold(), e["ports"]))


def known_ports(checks):
    """The ports of what the page shows already: the tiles' apps and our ntfy server."""
    ports = {port for cid, _, _, port in TILES if cid in checks}
    if server := (checks.get("ntfy") or {}).get("server"):
        try:
            ports.add(urllib.parse.urlsplit(server).port or 80)
        except ValueError:
            pass
    return ports


def disk_usage(path):
    st = os.statvfs(path)
    used = (st.f_blocks - st.f_bfree) * st.f_frsize
    free = st.f_bavail * st.f_frsize  # what we can write (df's "Avail")
    return used, free, round(used * 100 / ((used + free) or 1))


def check_storage(s):
    root = s["storage_root"]
    if s["require_mount"] and not os.path.ismount(root):
        return result(DOWN, f"{root} is not mounted: nothing can be downloaded or played")
    try:
        used, free, percent = disk_usage(root)
    except OSError as e:
        return result(DOWN, f"{root}: {e.strerror}")
    state = WARN if percent >= s["disk_warn_percent"] else OK
    return result(state, f"{human_size(free)} free of {human_size(used + free)} ({percent}% used)",
                  percent=percent)


def read_attrlog(path):
    """The latest line of a smartd attribute log -> {attribute id: raw value}. A line is
    "date time;" then "id;normalized;raw;" per attribute; raw may be "35 (Min/Max 18/45)"."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - 8192))  # the last lines are enough
        lines = f.read().decode(errors="replace").splitlines()
    last = next((line for line in reversed(lines) if line.strip()), "")
    values = [v.strip() for v in last.split(";")[1:]]
    attrs = {}
    for i in range(0, len(values) - 2, 3):
        raw = re.match(r"\d+", values[i + 2])
        if values[i].isdigit() and raw:
            attrs[int(values[i])] = int(raw[0])
    return attrs


DRIVEDBS = [Path("/var/lib/smartmontools/drivedb/drivedb.h"),  # update-smart-drivedb's
            Path("/usr/share/smartmontools/drivedb.h")]
SYS_BLOCK, UDEV_DATA = Path("/sys/class/block"), Path("/run/udev/data")
DRIVEDB_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|//[^\n]*|/\*.*?\*/|([{},])', re.S)


@functools.lru_cache(maxsize=None)
def drive_families():
    """(family, model regex) pairs from smartmontools' drive database."""
    for path in DRIVEDBS:
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        entries, fields, depth = [], [], 0
        for m in DRIVEDB_TOKEN.finditer(text):
            string, punct = m.group(1), m.group(2)
            if punct == "{":
                depth, fields, field = depth + 1, [], ""
            elif punct == "}":
                if depth == 1 and len(fields) >= 1:
                    entries.append(fields + [field])
                depth -= 1
            elif punct == "," and depth == 1:
                fields.append(field)
                field = ""
            elif string is not None and depth == 1:
                field += string  # "a" "b" is one string in C
        families = []
        for family, regex, *_ in entries:
            if not family or family.startswith(("$", "USB:", "VERSION:", "DEFAULT")):
                continue
            try:
                families.append((family, re.compile(regex)))
            except re.error:
                continue
        if families:
            return families
    return []


def drive_family(model):
    """'ST12000VN0008-3MH101' -> 'Seagate IronWolf', or None if the database doesn't know."""
    return next((family for family, regex in drive_families() if regex.fullmatch(model)), None)


def find_disk(model, serial):
    """(name, device) of a disk: its family and size ("Seagate IronWolf 12 TB"), or its
    model if unknown; the device ("sda") or None. smartd's log names have the model with
    anything but letters and digits as "_"; udev's ID_SERIAL has it with only spaces as "_"
    (sysfs cuts it at 16 characters). Not /dev/disk/by-id: the service has no /dev."""
    size = dev = None
    for block in SYS_BLOCK.glob("*"):
        if (block / "partition").exists():  # same serial as its disk
            continue
        try:
            udev = dict(re.findall(r"^E:(\w+)=(.*)$",
                                   (UDEV_DATA / f"b{(block / 'dev').read_text().strip()}")
                                   .read_text(), re.M))
            if udev.get("ID_SERIAL_SHORT") != serial or "ID_SERIAL" not in udev:
                continue
            model, dev = udev["ID_SERIAL"][:-len(serial) - 1].replace("_", " "), block.name
            size = int((block / "size").read_text()) * 512
        except (OSError, ValueError):
            continue
        break
    family = drive_family(model)
    if not family:
        return model, dev
    if size:
        family += f" {size / 1e12:.0f} TB" if size >= 1e12 else f" {size / 1e9:.0f} GB"
    return family, dev


DISK_IO = {}  # device -> Deltas of its sectors read, written and time busy


def disk_io(dev, now):
    """{read, write (bytes/s), busy (%), history [(time, read, write, busy)]} of a disk from
    its counters in sysfs; read, write and busy are None until the second look."""
    fields = [int(v) for v in (SYS_BLOCK / dev / "stat").read_text().split()]
    counters = (fields[2], fields[6], fields[9])  # sectors (512 bytes) read, written; ms busy

    def rates(seconds, changes):
        read, written, busy = changes
        return read * 512 / seconds, written * 512 / seconds, min(100, busy / 10 / seconds)
    deltas = DISK_IO.setdefault(dev, Deltas())
    moved = deltas.update(now, counters)
    read, write, busy = rates(*moved) if moved else (None, None, None)
    return {"read": read, "write": write, "busy": busy,
            "history": [(t, *rates(seconds, changes)) for t, seconds, changes, _ in deltas.series()]}


def check_disk(s, now=None):
    now = now or time.time()
    state, summary = service_check("smartmontools")
    if state != OK:
        return result(state, f"smartd {summary}: nobody watches the disks' health")
    try:
        logs = [p for p in sorted(SMART_DIR.glob("attrlog.*.ata.csv"))
                if now - p.stat().st_mtime < 2 * 3600]  # disks smartd watches now
    except OSError:
        logs = []
    if not logs:
        return result(OK, "smartd is watching (no readings yet)")
    parts, details, state, io = [], [], OK, []
    for log in logs:
        model, serial = log.name[len("attrlog."):-len(".ata.csv")].rsplit("-", 1)
        model, dev = find_disk(model, serial)
        try:
            attrs = read_attrlog(log)
        except OSError:
            continue
        if dev:
            try:
                io.append({"name": model, **disk_io(dev, now)})
            except (OSError, ValueError, IndexError):
                pass
        temp = attrs.get(194, attrs.get(190))
        part = model
        if temp is not None:
            temp &= 0xFF  # some disks pack min/max into the raw value
            part += f", {temp} °C"
            if temp >= DISK_HOT:
                state = WARN
                details.append(f"{model} is hot ({temp} °C)")
        bad = [(attrs[aid], label) for aid, label in SMART_BAD if attrs.get(aid)]
        if bad:
            state = WARN
            details.append(f"{model}: " + ", ".join(f"{n} {label}" for n, label in bad)
                           + " sectors: back up what you can't download again")
        else:
            part += ", no bad sectors"
        parts.append(part)
    return result(state, "; ".join(parts) or "smartd is watching", details, io=io)


def cpu_temperature():
    """CPU temperature in °C from hwmon (AMD, Intel, Raspberry Pi), or None."""
    for hwmon in sorted(Path("/sys/class/hwmon").glob("hwmon*")):
        try:
            if (hwmon / "name").read_text().strip() in ("k10temp", "coretemp", "cpu_thermal"):
                return int((hwmon / "temp1_input").read_text()) / 1000
        except (OSError, ValueError):
            continue
    return None


def cpu_times():
    """(busy, total) jiffies of all CPUs together, from /proc/stat."""
    fields = [int(v) for v in Path("/proc/stat").read_text().split("\n", 1)[0].split()[1:]]
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0)  # idle + iowait
    total = sum(fields[:8])  # guest time is in user time already
    return total - idle, total


class Deltas:
    """How counters (CPU time, a disk's sectors...) moved since the last look, and in each
    minute of the last hour for the sparklines (the watcher looks every WATCH_EVERY, page or
    not)."""
    SAMPLE_EVERY, KEEP = 60, 60

    def __init__(self):
        self.lock = threading.Lock()
        self.last = self.sampled = None  # (time, counters)
        self.history = collections.deque(maxlen=self.KEEP)  # (time, seconds, changes, extra)

    def update(self, now, counters, extra=None):
        """(seconds, changes) since the last look, or None the first time (or when a counter
        went back). Once a minute it also keeps the minute's, with extra, in the history."""
        with self.lock:
            last, self.last = self.last, (now, counters)
            if not self.sampled or now - self.sampled[0] >= self.SAMPLE_EVERY - 5:
                minute = self.change(self.sampled, now, counters)
                if minute:
                    self.history.append((now, *minute, extra))
                self.sampled = (now, counters)
            return self.change(last, now, counters)

    @staticmethod
    def change(since, now, counters):
        if not since or now <= since[0]:
            return None
        changes = [new - old for new, old in zip(counters, since[1])]
        return None if any(c < 0 for c in changes) else (now - since[0], changes)

    def series(self):
        with self.lock:
            return list(self.history)


CPU = Deltas()  # of (busy, total) CPU time; with memory % as the extra


def cpu_percent(changes):
    busy, total = changes
    return busy * 100 / total if total > 0 else None


def memory_info():
    """{total, used, cache, free} in bytes, from /proc/meminfo.
    used + cache + free = total; cache is what the kernel gives back when apps need it."""
    mem = {k: int(v) * 1024 for k, v in
           re.findall(r"^(\w+):\s+(\d+)", Path("/proc/meminfo").read_text(), re.M)}
    total, available, free = mem["MemTotal"], mem["MemAvailable"], mem["MemFree"]
    return {"total": total, "used": total - available, "cache": max(0, available - free),
            "free": free}


def check_system(s, now=None):
    now = now or time.time()
    parts, state, metrics = [], OK, {"cpus": os.cpu_count()}
    try:
        uptime = float(Path("/proc/uptime").read_text().split()[0])
        load = Path("/proc/loadavg").read_text().split()[0]
        mem = memory_info()
        parts += [f"up {duration(uptime)}", f"load {load} ({os.cpu_count()} CPUs)",
                  f"memory {human_size(mem['used'])} of {human_size(mem['total'])}"]
        metrics.update(uptime=uptime, load=float(load), memory=mem)
        moved = CPU.update(now, cpu_times(), mem["used"] * 100 / mem["total"])
        metrics["cpu"] = cpu_percent(moved[1]) if moved else None
    except (OSError, ValueError, KeyError, IndexError, ZeroDivisionError):
        pass
    metrics["history"] = [(t, cpu, mem) for t, _, changes, mem in CPU.series()
                          if (cpu := cpu_percent(changes)) is not None]
    temp = cpu_temperature()
    if temp is not None:
        parts.append(f"CPU {temp:.0f} °C")
        metrics["temp"] = temp
    details = []
    used, free, percent = disk_usage("/")
    metrics["disk"] = {"used": used, "free": free, "percent": percent}
    if percent >= 90:
        state = WARN
        details.append(f"system disk {percent}% full ({human_size(free)} free)")
    return result(state, ", ".join(parts) or "?", details, metrics=metrics)


def gather(s, arr_dir, pool, last_login=None):
    checks = {"storage": check_storage, "disk": check_disk, "transmission": check_transmission}
    if s["media_server"] in ("jellyfin", "minidlna"):
        checks[s["media_server"]] = globals()[f"check_{s['media_server']}"]
    if s["arr"]:
        checks["seerr"] = check_seerr
        for name in ARR_APPS:
            if name != "whisparr" or s["whisparr"]:
                checks[name] = lambda s, name=name: check_arr(name, arr_dir)
        checks["ncore"] = lambda s: check_ncore(arr_dir, last_login)
        if s["whisparr"]:  # Desireerr comes with Whisparr
            checks["desireerr"] = lambda s: check_desireerr(s, arr_dir)
    checks["rss2transmission"] = check_rss2transmission
    if s["samba"]:
        checks["samba"] = check_samba
    if s["own_ntfy"]:
        checks["ntfy"] = check_ntfy
    if s["tailscale"]:
        checks["tailscale"] = check_tailscale
    checks["system"] = check_system

    def safely(check):
        try:
            return check(s)
        except Exception as e:  # one broken check mustn't take the page down
            return result(WARN, f"check failed: {e!r}")

    futures = {name: pool.submit(safely, check) for name, check in checks.items()}
    return {"time": time.time(), "hostname": socket.gethostname(),
            "storage_root": str(s["storage_root"]),
            "checks": {name: f.result() for name, f in futures.items()}}


### Pushes

class Watcher:
    """Which checks have been bad for ALERT_AFTER (-> "down" once) and have recovered since
    (-> "back")."""

    def __init__(self, delay=ALERT_AFTER):
        self.delay, self.since, self.alerted = delay, {}, set()

    def update(self, checks, now):
        events = []
        for cid, c in checks.items():
            floor = ALERT_FROM.get(cid, WARN)
            if floor is not None and WORST[c["state"]] >= WORST[floor]:
                since = self.since.setdefault(cid, now)
                if cid not in self.alerted and now - since >= self.delay:
                    self.alerted.add(cid)
                    events.append(("down", cid, c))
            else:
                self.since.pop(cid, None)
                if cid in self.alerted:
                    self.alerted.discard(cid)
                    events.append(("back", cid, c))
        for cid in set(self.since) - set(checks):  # no longer checked (setup changed)
            self.since.pop(cid)
            self.alerted.discard(cid)
        return events

    def retry(self, cid):
        """The push failed: send it again next round."""
        self.alerted.discard(cid)


def push_event(s, kind, cid, c):
    """A push for a Watcher event; False if it couldn't be sent."""
    name = dict((i, n) for i, n, *_ in TILES + ROWS).get(cid, cid)
    if kind == "down":
        title = f"{name} is down" if c["state"] == DOWN else f"{name} needs a look"
        message = "\n".join([c["summary"], *c["details"], *upstream_lines(c)])
        priority, tags = s["priority_problem"], ["rotating_light" if c["state"] == DOWN else "warning"]
    else:
        title, message, priority, tags = f"{name} is fine again", c["summary"], 3, ["white_check_mark"]
    return send_push(s, title, message, priority, tags)


def send_push(s, title, message, priority, tags, click=None):
    """False if it couldn't be sent to ntfy; True also when it's off (no topic). Browsers
    with the page installed get it too, whatever ntfy did."""
    if WEBPUSH:
        WEBPUSH.send(title, message)
    if not s["ntfy_topic"]:
        return True
    body = {"topic": s["ntfy_topic"], "title": title, "message": message, "priority": priority,
            "tags": tags, "click": click or f"http://{socket.gethostname()}/"}
    req = urllib.request.Request(s["ntfy_server"], data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
    except OSError as e:
        print(f"push '{title}' failed: {problem(e)}", file=sys.stderr, flush=True)
        return False
    print(f"pushed: {title}", file=sys.stderr, flush=True)
    return True


### Web Push

# Where a browser's push service lives; the box only ever posts to these, never to an
# address typed into a form.
PUSH_HOSTS = (".googleapis.com", ".push.services.mozilla.com", ".push.apple.com",
              ".notify.windows.com")
PUSH_DEVICES = 20          # subscribed browsers at most
PUSH_TEST_EVERY = 10       # seconds between two test pushes
PUSH_HEADER = "X-Requested-With"  # a form on another site can't send it without our say-so


def b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def unb64url(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def push_crypto():
    """The `cryptography` package (apt: python3-cryptography), which Web Push's encryption
    needs; None without it, and the page then offers no push to devices."""
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    except ImportError:
        return None
    return {"hashes": hashes, "serialization": serialization, "ec": ec, "AESGCM": AESGCM,
            "HKDF": HKDF, "decode_dss_signature": decode_dss_signature}


class WebPush:
    """Push notifications to the browsers where the start page is installed (RFC 8030 push,
    RFC 8291 encryption, RFC 8292 VAPID), next to the ntfy ones. The VAPID key, the
    subscriptions and a token for the root jobs live in state_dir (none: kept until restart)."""

    def __init__(self, state_dir=None):
        self.c = push_crypto()
        self.dir = Path(state_dir) if state_dir else None
        self.lock = threading.Lock()
        self.subs = []          # {"endpoint", "p256dh", "auth", "label"}
        self.key = None
        self.token = secrets.token_urlsafe(24)
        self.tested = 0.0
        if self.dir:
            try:
                self.subs = json.loads((self.dir / "push.json").read_text())
            except (OSError, ValueError):
                pass
            try:
                self.token = (self.dir / "push-token").read_text().strip() or self.token
            except OSError:
                self.write("push-token", self.token)

    @property
    def supported(self):
        return self.c is not None

    def write(self, name, text):
        if not self.dir:
            return
        try:
            path = self.dir / name
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(text)
        except OSError as e:
            print(f"can't keep {name}: {e}", file=sys.stderr, flush=True)

    def private_key(self):
        if self.key is None:
            ec = self.c["ec"]
            try:
                saved = (self.dir / "vapid.key").read_text().strip() if self.dir else ""
            except OSError:
                saved = ""
            if saved:
                self.key = ec.derive_private_key(int.from_bytes(unb64url(saved), "big"), ec.SECP256R1())
            else:
                self.key = ec.generate_private_key(ec.SECP256R1())
                self.write("vapid.key", b64url(self.key.private_numbers().private_value.to_bytes(32, "big")))
        return self.key

    def public_key(self):
        sr = self.c["serialization"]
        return b64url(self.private_key().public_key().public_bytes(
            sr.Encoding.X962, sr.PublicFormat.UncompressedPoint))

    @staticmethod
    def endpoint_ok(url):
        try:
            parts = urllib.parse.urlsplit(url)
            host = (parts.hostname or "").lower()
            return (parts.scheme == "https" and not parts.username and not parts.port
                    and any(host.endswith(h) for h in PUSH_HOSTS))
        except ValueError:
            return False

    def save(self):
        self.write("push.json", json.dumps(self.subs))

    def subscribe(self, body):
        """Remember a browser's subscription (again, if it's known); ValueError if it's not one."""
        endpoint = str(body.get("endpoint") or "")
        keys = body.get("keys") or {}
        p256dh, auth = str(keys.get("p256dh") or ""), str(keys.get("auth") or "")
        if not self.endpoint_ok(endpoint):
            raise ValueError("not a push service address")
        try:
            if len(unb64url(p256dh)) != 65 or len(unb64url(auth)) != 16:
                raise ValueError
        except ValueError:
            raise ValueError("invalid keys") from None
        label = re.sub(r"\s+", " ", str(body.get("label") or "")).strip()[:60]
        with self.lock:
            self.subs = [x for x in self.subs if x["endpoint"] != endpoint]
            if len(self.subs) >= PUSH_DEVICES:
                raise ValueError(f"{PUSH_DEVICES} devices is the most")
            self.subs.append({"endpoint": endpoint, "p256dh": p256dh, "auth": auth, "label": label})
            self.save()

    def unsubscribe(self, endpoint):
        with self.lock:
            self.subs = [x for x in self.subs if x["endpoint"] != endpoint]
            self.save()

    def encrypt(self, sub, data):
        """The body of a push, aes128gcm (RFC 8291) for a subscription's keys."""
        c = self.c
        ec, hashes, sr = c["ec"], c["hashes"], c["serialization"]
        ua_public = unb64url(sub["p256dh"])
        ephemeral = ec.generate_private_key(ec.SECP256R1())
        as_public = ephemeral.public_key().public_bytes(sr.Encoding.X962, sr.PublicFormat.UncompressedPoint)
        shared = ephemeral.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(
            ec.SECP256R1(), ua_public))
        ikm = c["HKDF"](hashes.SHA256(), 32, unb64url(sub["auth"]),
                        b"WebPush: info\0" + ua_public + as_public).derive(shared)
        salt = os.urandom(16)
        key = c["HKDF"](hashes.SHA256(), 16, salt, b"Content-Encoding: aes128gcm\0").derive(ikm)
        nonce = c["HKDF"](hashes.SHA256(), 12, salt, b"Content-Encoding: nonce\0").derive(ikm)
        sealed = c["AESGCM"](key).encrypt(nonce, data + b"\x02", None)
        return salt + (4096).to_bytes(4, "big") + bytes([len(as_public)]) + as_public + sealed

    def vapid(self, endpoint):
        """The Authorization header that tells the push service who is pushing (RFC 8292)."""
        c = self.c
        parts = urllib.parse.urlsplit(endpoint)
        claims = {"aud": f"{parts.scheme}://{parts.netloc}", "exp": int(time.time()) + 12 * 3600,
                  "sub": f"https://github.com/{GITHUB_REPO}"}
        head = b64url(json.dumps({"typ": "JWT", "alg": "ES256"}).encode())
        text = f"{head}.{b64url(json.dumps(claims).encode())}"
        r, sg = c["decode_dss_signature"](self.private_key().sign(
            text.encode(), c["ec"].ECDSA(c["hashes"].SHA256())))
        return f"vapid t={text}.{b64url(r.to_bytes(32, 'big') + sg.to_bytes(32, 'big'))}, k={self.public_key()}"

    def deliver(self, sub, data):
        """One push; a browser that has forgotten the subscription (404, 410) is dropped.
        The reason for a failure, "" when it went through."""
        try:
            req = urllib.request.Request(
                sub["endpoint"], data=self.encrypt(sub, data), method="POST",
                headers={"Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream",
                         "TTL": str(24 * 3600), "Urgency": "high", "User-Agent": "torrent-box-dashboard",
                         "Authorization": self.vapid(sub["endpoint"])})
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp.read()
            return ""
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                self.unsubscribe(sub["endpoint"])
                return "the device has unsubscribed"
            reason = f"the push service answered {e.code}"
        except OSError as e:
            reason = f"can't reach the push service: {problem(e)}"
        print(f"web push failed: {reason}", file=sys.stderr, flush=True)
        return reason

    def send(self, title, message, tag="", wait=False):
        """Push to every subscribed browser; in the background unless wait. How many were
        reached (with wait)."""
        if not self.supported or not self.subs:
            return 0
        data = json.dumps({"title": title[:120], "body": message[:600], "tag": tag or None,
                           "url": "/"}).encode()
        subs = list(self.subs)
        if not wait:
            for sub in subs:
                threading.Thread(target=self.deliver, args=(sub, data), daemon=True).start()
            return len(subs)
        return sum(1 for sub in subs if not self.deliver(sub, data))

    def test(self, now=None):
        """A test push now; ValueError when there's no one to send to or it was too soon."""
        now = time.monotonic() if now is None else now
        if not self.subs:
            raise ValueError("no device has subscribed")
        if now - self.tested < PUSH_TEST_EVERY:
            raise ValueError("wait a few seconds")
        self.tested = now
        if not self.send("torrent-box", "This is a test.", "test", wait=True):
            raise ValueError("it didn't reach any device")


# Set in main(): the start page's own pushes go to subscribed browsers as well as to ntfy.
WEBPUSH = None

SERVICE_WORKER = """// torrent-box's service worker: it only shows pushes, and brings the page forward on a tap.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data.json(); } catch (err) { /* shown plain */ }
  e.waitUntil(self.registration.showNotification(d.title || "torrent-box", {
    body: d.body || "", tag: d.tag || undefined, icon: "/app/icon-192.png",
    badge: "/app/badge-96.png", data: { url: d.url || "/" },
  }));
});
self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const url = new URL(e.notification.data.url || "/", self.location.origin).href;
  e.waitUntil((async () => {
    for (const c of await self.clients.matchAll({ type: "window", includeUncontrolled: true })) {
      if (c.url.startsWith(self.location.origin) && "focus" in c) {
        await c.focus();
        if ("navigate" in c) await c.navigate(url).catch(() => {});
        return;
      }
    }
    await self.clients.openWindow(url);
  })());
});
""".encode()


### Updates

def github(path, timeout=15):
    """GitHub's REST API for the repo (no token: 60 requests an hour per address)."""
    return json.loads(http_get(f"https://api.github.com/repos/{GITHUB_REPO}/{path}", headers={
        "Accept": "application/vnd.github+json", "User-Agent": "torrent-box-dashboard"},
        timeout=timeout))


def installed(path=None):
    """INSTALLED_FILE's {"commit", "date", "repo"}; {} when it isn't there."""
    try:
        info = json.loads(Path(path or INSTALLED_FILE).read_text(encoding="utf-8"))
        return info if isinstance(info, dict) else {}
    except (OSError, ValueError):
        return {}


def version_text():
    """'1.0 rev 2, 2026-11-09 13:31': the version and when its commit was made (as setup.sh
    shows it); "" when not installed by setup.sh."""
    date = installed().get("date")
    return f"{VERSION}, {date}" if VERSION and date else VERSION


def remote_version(fetch, head, head_date):
    """The version setup.sh would install from BRANCH: VERSION at its newest commit HEAD, and
    that commit's time (as version_text)."""
    number = base64.b64decode(fetch(f"contents/VERSION?ref={head}")["content"]).decode().strip()
    when = time.localtime(calendar.timegm(time.strptime(head_date, "%Y-%m-%dT%H:%M:%SZ")))
    return f"{number}, {time.strftime('%Y-%m-%d %H:%M', when)}"


def check_update(commit=None, fetch=github):
    """None when there's nothing to compare (not installed from git); else what BRANCH has
    that the installed commit hasn't: {"commits": 0} when nothing."""
    commit = installed().get("commit", "") if commit is None else commit
    if not commit:
        return None
    diff = fetch(f"compare/{commit}...{BRANCH}")
    if not diff.get("ahead_by"):
        return {"commits": 0}
    commits = diff["commits"]  # oldest first
    return {"commits": diff["ahead_by"], "head": commits[-1]["sha"],
            "version": remote_version(fetch, commits[-1]["sha"],
                                      commits[-1]["commit"]["committer"]["date"]),
            "subjects": [c["commit"]["message"].split("\n", 1)[0] for c in reversed(commits)]
                        [:UPDATE_SUBJECTS],
            "url": diff.get("html_url") or f"https://github.com/{GITHUB_REPO}"}


def update_command():
    where = installed().get("repo") or "torrent-box"
    home = os.path.expanduser("~")
    if home != "/" and (where == home or where.startswith(home + "/")):
        where = "~" + where[len(home):]
    return f"cd {where} && git pull && ./setup.sh"


class Updates:
    """Asks GitHub every UPDATE_EVERY (UPDATE_RETRY after a failure); pushes a new head once,
    at most every UPDATE_PUSH_EVERY. What was pushed is kept in state_dir/update.json, so a
    restart doesn't push it again (no state_dir: remembered until then)."""

    def __init__(self, state_dir=None, check=check_update):
        self.check, self.info, self.due = check, None, 0.0
        self.path = Path(state_dir) / "update.json" if state_dir else None
        self.pushed = {}
        try:
            self.pushed = json.loads(self.path.read_text()) if self.path else {}
        except (OSError, ValueError):
            pass

    def poll(self, settings, now):
        if now < self.due:
            return
        try:
            self.info = self.check()
            self.due = now + UPDATE_EVERY
        except (OSError, ValueError, LookupError) as e:
            print(f"update check failed: {problem(e) if isinstance(e, OSError) else repr(e)}",
                  file=sys.stderr, flush=True)
            self.due = now + UPDATE_RETRY
            return
        info = self.info
        if not info or not info["commits"] or info["head"] == self.pushed.get("head") \
                or now - self.pushed.get("time", now - UPDATE_PUSH_EVERY) < UPDATE_PUSH_EVERY:
            return
        n = info["commits"]
        message = "\n".join([f"{n} new commit{'s' if n > 1 else ''} (you have {version_text() or '?'}):",
                             *(f"• {line}" for line in info["subjects"]),
                             *([f"… and {n - len(info['subjects'])} more"]
                               if n > len(info["subjects"]) else []),
                             f"To update: {update_command()}"])
        if send_push(settings, f"torrent-box {info['version']} is available", message, 3,
                     ["arrow_up"], info["url"]):
            self.pushed = {"head": info["head"], "time": now}
            if self.path:
                try:
                    self.path.write_text(json.dumps(self.pushed))
                except OSError as e:
                    print(f"can't remember the update push: {e}", file=sys.stderr, flush=True)


### Logos

class Logos:
    """The apps' logos, loaded once (a missing one is retried after LOGO_RETRY)."""

    def __init__(self, sources=None):
        self.sources = LOGOS if sources is None else sources
        self.found = {}  # app -> (bytes, content type)
        self.missed = {}  # app -> when it was last looked for in vain

    def get(self, app):
        if app in self.found or app not in self.sources:
            return self.found.get(app)
        if time.monotonic() - self.missed.get(app, -LOGO_RETRY) < LOGO_RETRY:
            return None
        for source in self.sources[app]:
            logo = self.load(source)
            if logo:
                self.found[app] = logo
                return logo
        self.missed[app] = time.monotonic()
        return None

    @staticmethod
    def load(source):
        kind = LOGO_TYPES.get(Path(urllib.parse.urlsplit(source).path).suffix.lower())
        if kind is None:
            return None
        try:
            if source.startswith("http://"):
                data = http_get(source)
            else:
                path = next(iter(sorted(Path("/").glob(source.lstrip("/")))), None)
                data = path.read_bytes() if path else b""
        except OSError:
            return None
        if not data or len(data) > LOGO_MAX:
            return None
        if kind == "image/svg+xml" and b"<svg" not in data[:4096]:
            return None  # e.g. an app's HTML page instead of its logo
        if kind == "image/png" and not data.startswith(b"\x89PNG"):
            return None
        return data, kind

    def available(self, apps, pool):
        """Which of these apps have a logo (loading the missing ones in parallel)."""
        found = pool.map(lambda app: (app, self.get(app) is not None), apps)
        return sorted(app for app, ok in found if ok)


### Page

HOST = re.compile(r"(?P<host>\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*)(?::\d+)?")


# The apps' own names on the tailnet, as Tailscale Services (svc:<name>), which setup.sh
# serves once the box is tagged; /tailscale shows the steps. (name, its tile's port; None:
# the start page.) Not Jellyfin: phones and TVs use its own app.
SERVICES = [("torrent-box", None), ("seerr", 5055), ("desireerr", 5056)]


def wanted_services(checks):
    """The SERVICES this box runs: the start page, and the apps it has a tile for."""
    return [(name, port) for name, port in SERVICES if port is None or name in checks]


def services_note(checks, https):
    """On a page opened over HTTPS (so on the tailnet): how many apps have their own name yet."""
    tailnet = checks.get("tailscale")
    if not https or not tailnet or tailnet["state"] != OK:
        return ""
    wanted = [name for name, _ in wanted_services(checks)]
    ready = [n for n in wanted if n in tailnet.get("approved", ()) and n in tailnet.get("served", {})]
    if len(ready) == len(wanted):
        return ""
    return (f'<p class="muted">Own names on the tailnet, so a phone installs each app separately: '
            f'{len(ready)} of {len(wanted)} ready · <a href="/tailscale">how to set them up</a></p>')


def app_url(host, port, https=False, tailnet=None):
    """An app on its own: its port on the host the page was opened with; over HTTPS, the app's
    own Tailscale Service if it has one, else its HTTPS port on the box's tailnet name. TAILNET:
    the tailscale check's result (name, services)."""
    tailnet = tailnet or {}
    by_port = port_url(host, port, https, tailnet)
    if https and (service := (tailnet.get("links") or {}).get(str(port))):
        # the script falls back to the port where this device can't reach the service
        return f'https://{html.escape(service)}/" data-fallback="{by_port}'
    return by_port


def link_host(header, fallback):
    """The host the browser used (so links work by IP, name or Tailscale name), if sane."""
    match = HOST.fullmatch((header or "").strip())
    return match["host"] if match else fallback


# torrent-box's mark: a box with a download arrow, in the brand gradient
BRAND = ("#4f5bd5", "#a14fd6")

# To install the page as an app on a phone (over HTTPS): its manifest and icons, drawn by
# tools/make_icons.py and installed by setup.sh (from a checkout: the repo's own).
WEBAPP_ICON_DIRS = [Path("/usr/local/share/torrent-box/icons"), Path(__file__).resolve().parent / "icons"]
WEBAPP_ICONS = ("icon-192.png", "icon-512.png", "icon-maskable-512.png", "icon-180.png",
                "badge-96.png")
MANIFEST = json.dumps({
    "id": "/", "name": "torrent-box", "short_name": "torrent-box", "start_url": "/", "scope": "/",
    "display": "standalone", "background_color": "#0e1017", "theme_color": BRAND[0],
    "icons": [{"src": "/app/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
              {"src": "/app/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
              {"src": "/app/icon-maskable-512.png", "sizes": "512x512", "type": "image/png",
               "purpose": "maskable"}],
}, indent=1).encode()


def webapp_icon(name):
    """One of WEBAPP_ICONS as PNG bytes, or None."""
    if name not in WEBAPP_ICONS:
        return None
    for folder in WEBAPP_ICON_DIRS:
        try:
            return (folder / name).read_bytes()
        except OSError:
            continue
    return None


def logo_svg(gid, dot=None):
    """The logo; dot: a status color for the favicon's corner."""
    corner = (f'<circle cx="39" cy="39" r="8" fill="{dot}" stroke="#fff" stroke-width="3"/>'
              if dot else "")
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48" aria-hidden="true">'
            f'<defs><linearGradient id="{gid}" x1="0" y1="0" x2="1" y2="1">'
            f'<stop offset="0" stop-color="{BRAND[0]}"/><stop offset="1" stop-color="{BRAND[1]}"/>'
            f'</linearGradient></defs><rect width="48" height="48" rx="12" fill="url(#{gid})"/>'
            '<g fill="none" stroke="#fff" stroke-width="3.2" stroke-linecap="round" '
            'stroke-linejoin="round"><path d="M13 24v9a3 3 0 0 0 3 3h16a3 3 0 0 0 3-3v-9"/>'
            f'<path d="M24 11v16m-6.5-6.5L24 27l6.5-6.5"/></g>{corner}</svg>')


COLORS = {OK: "#1f9d55", WARN: "#d98a00", DOWN: "#d64545"}


def favicon(state):
    return "data:image/svg+xml," + urllib.parse.quote(logo_svg("f", COLORS[state]))


# Simple glyphs of our own (24x24, stroked), not the apps' logos; tiles get the app's colors.
APP_ICONS = {
    "jellyfin": ("#aa5cc3,#00a4dc", '<path d="M9 6.5v11l9-5.5z" fill="#fff"/>'),
    "minidlna": ("#2f9e8f,#1f6f8b", '<path d="M4 13a8 8 0 0 1 16 0M7.5 13a4.5 4.5 0 0 1 9 0"/>'
                                    '<circle cx="12" cy="16.5" r="1.6" fill="#fff"/>'),
    "seerr": ("#6366f1,#8b5cf6", '<path d="m12 3.5 2.6 5.3 5.9.9-4.3 4.1 1 5.8-5.2-2.7-5.2 2.7 '
                                 '1-5.8-4.3-4.1 5.9-.9z" fill="#fff"/>'),
    "transmission": ("#e0323f,#a4161a", '<path d="M8 4v13m-4-4 4 4 4-4M16 20V7m-4 4 4-4 4 4"/>'),
    "sonarr": ("#2bbbe9,#1b78c2", '<rect x="3" y="6" width="18" height="12" rx="2"/>'
                                  '<path d="M8.5 21h7M9 2.5l3 3.5 3-3.5"/>'),
    "radarr": ("#f7c133,#e3870c", '<rect x="3" y="4" width="18" height="16" rx="2"/>'
                                  '<path d="M7.5 4v16M16.5 4v16M3 9h4.5M3 15h4.5M16.5 9H21M16.5 15H21"/>'),
    "prowlarr": ("#f0802a,#cf4a0e", '<circle cx="10.5" cy="10.5" r="6"/><path d="m19.5 19.5-4.6-4.6"/>'),
    "desireerr": ("#fb7185,#be123c", '<path d="M12 3c1.2 3.8 5.4 6.2 5.4 11.6a5.4 5.4 0 0 1-10.8 0c0-2.4 1.2-4 2.8-5.6.2 2 1 3.2 2 3.6-.8-3-.4-6.6.6-9.6z"/>'),
    "whisparr": ("#f472b6,#be185d", '<path d="M12 20s-7.5-4.6-7.5-10A4.3 4.3 0 0 1 12 7.4 '
                                    '4.3 4.3 0 0 1 19.5 10c0 5.4-7.5 10-7.5 10z"/>'),
}
ROW_ICONS = {
    "storage": '<ellipse cx="12" cy="6" rx="8" ry="3"/>'
               '<path d="M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
    "rss2transmission": '<path d="M5 11a8 8 0 0 1 8 8M5 5a14 14 0 0 1 14 14"/>'
                        '<circle cx="6" cy="18" r="1.4" fill="currentColor"/>',
    "ncore": '<circle cx="8" cy="15.5" r="4.5"/><path d="m11.2 12.3 8.8-8.8m-3.5 3.5 3 3"/>',
    "samba": '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    "tailscale": '<circle cx="12" cy="5" r="2"/><circle cx="5" cy="19" r="2"/><circle cx="19" cy="19" r="2"/>'
                 '<path d="M11 6.8 6 17.2M13 6.8l5 10.4M7 19h10"/>',
    "disk": '<path d="M3 12h4l2.5-6 5 12 2.5-6h4"/>',
    "ntfy": '<path d="M6 16V11a6 6 0 0 1 12 0v5l1.5 2h-15zM10 20.5a2 2 0 0 0 4 0"/>',
    "system": '<rect x="6" y="6" width="12" height="12" rx="2"/>'
              '<path d="M9.5 2.5V6M14.5 2.5V6M9.5 18v3.5M14.5 18v3.5M2.5 9.5H6M2.5 14.5H6M18 9.5h3.5M18 14.5h3.5"/>',
}


def glyph(paths, color):
    return (f'<svg viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" '
            f'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{paths}</svg>')


CSS = """
:root{color-scheme:light dark;--bg:#f4f5fa;--card:#fff;--fg:#1b1f2e;--muted:#646b80;
--line:#e3e5ee;--soft:#eef0fb;--ok:#1f9d55;--warn:#d98a00;--down:#d64545;--accent:#4f5bd5;
--b1:%s;--b2:%s;--shadow:0 1px 2px rgba(20,24,60,.05),0 4px 14px rgba(20,24,60,.05)}
@media (prefers-color-scheme:dark){:root{--bg:#0e1017;--card:#171a24;--fg:#e7e9f2;
--muted:#9aa1b5;--line:#272b3a;--soft:#20243a;--accent:#8d95ff;--shadow:none}}
*{box-sizing:border-box}
body{margin:0;font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;color:var(--fg);
background-color:var(--bg);background-repeat:no-repeat;
background-image:radial-gradient(900px 340px at 0 -80px,color-mix(in srgb,var(--b1) 16%%,transparent),transparent),
radial-gradient(700px 300px at 100%% -120px,color-mix(in srgb,var(--b2) 12%%,transparent),transparent)}
.wrap{max-width:1000px;margin:0 auto;padding:28px 18px 36px}
header.top{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:14px}
.brand{display:flex;align-items:center;gap:14px}
.brand svg{width:48px;height:48px;flex:none;filter:drop-shadow(0 6px 14px color-mix(in srgb,var(--b1) 35%%,transparent))}
.word{font-size:26px;font-weight:750;letter-spacing:-.02em;line-height:1.1}
.word span{background:linear-gradient(90deg,var(--b1),var(--b2));-webkit-background-clip:text;
background-clip:text;color:transparent}
.host{color:var(--muted);font-size:14px;margin-top:2px}
.pill{display:inline-flex;align-items:center;padding:7px 14px;border-radius:999px;font-weight:600;
font-size:14px;background:var(--card);border:1px solid var(--line);box-shadow:var(--shadow)}
h2{font-size:12px;text-transform:uppercase;letter-spacing:.09em;color:var(--muted);margin:30px 0 10px}
.muted{color:var(--muted);font-size:13px}
.banner{margin-top:18px;padding:12px 16px;border-radius:14px;background:var(--card);box-shadow:var(--shadow);
border:1px solid var(--line);border-left:5px solid var(--warn)}
.banner.down{border-left-color:var(--down)}
.banner.update{border-left-color:var(--accent)}
.banner code{font-size:13px}
.banner ul{margin:6px 0 0;padding-left:20px}
.groups{display:grid;grid-template-columns:repeat(var(--cols),minmax(0,1fr));gap:12px}
.grp{grid-column:span var(--n);display:grid;grid-template-columns:subgrid;row-gap:8px;align-content:start}
.grp h3,.folded h3{font-size:11px;font-weight:650;text-transform:uppercase;letter-spacing:.09em;
color:var(--muted);margin:0;white-space:nowrap}
.grp h3{grid-column:1/-1;padding:0 4px 5px;border-bottom:2px solid color-mix(in srgb,var(--accent) 30%%,var(--line))}
.line{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-variant-numeric:tabular-nums}
.folded{margin-top:14px;background:var(--card);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow)}
.folded summary{display:flex;align-items:center;flex-wrap:wrap;gap:8px 12px;margin:0;padding:10px 16px;list-style:none}
.folded summary::-webkit-details-marker{display:none}
.chev{color:var(--muted);font-size:18px;line-height:1;transition:transform .15s}
.folded[open] .chev{transform:rotate(90deg)}
.faces{display:flex;gap:8px}
.face{position:relative;display:grid}
.face .logo,.face .badge{width:22px;height:22px;border-radius:6px}.face .badge svg{width:13px;height:13px}
.face .dot{position:absolute;right:-3px;bottom:-3px;width:8px;height:8px;box-shadow:0 0 0 2px var(--card)}
.face .dot.ok{display:none}
.wrong{display:flex;align-items:center;flex-wrap:wrap;gap:4px 10px;font-size:13px;color:var(--down)}
.wrong svg{width:16px;height:16px}
.folded .apps{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:2px 10px;
padding:6px 10px 10px;border-top:1px solid var(--line)}
.others{background:var(--card);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow)}
.others .apps{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:2px 10px;padding:6px 10px}
.badge.other{background:linear-gradient(135deg,#64748b,#334155)}
.app{display:flex;gap:10px;align-items:center;padding:8px;border-radius:10px;color:inherit;text-decoration:none}
.app:hover{background:var(--soft)}
.app>div{flex:1;min-width:0;font-size:14px}.app .line{font-size:12px}
.app .logo,.app .badge{width:30px;height:30px;border-radius:8px}.app .badge svg{width:17px;height:17px}
.tile{display:flex;gap:14px;align-items:flex-start;padding:16px;background:var(--card);
border:1px solid var(--line);border-radius:14px;color:inherit;text-decoration:none;
box-shadow:var(--shadow);transition:transform .12s,border-color .12s}
a.tile:hover{border-color:var(--accent);transform:translateY(-2px)}
.badge{width:44px;height:44px;border-radius:12px;display:grid;place-items:center;flex:none}
.badge svg{width:24px;height:24px}
.logo{width:44px;height:44px;flex:none;object-fit:contain}
.tile>div{flex:1;min-width:0}
.tile .name{display:flex;align-items:center;justify-content:space-between;gap:8px}
.tile b{font-size:17px}
.dot{display:inline-block;width:10px;height:10px;border-radius:50%%;flex:none;background:var(--muted)}
.dot.ok{background:var(--ok)}.dot.warn{background:var(--warn)}.dot.down{background:var(--down)}
.pill .dot{margin-right:8px}
.dot.ok{box-shadow:0 0 0 3px color-mix(in srgb,var(--ok) 18%%,transparent)}
.dot.warn{box-shadow:0 0 0 3px color-mix(in srgb,var(--warn) 22%%,transparent)}
.dot.down{box-shadow:0 0 0 3px color-mix(in srgb,var(--down) 22%%,transparent)}
.rows{background:var(--card);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow)}
.row{display:grid;grid-template-columns:200px 1fr;gap:4px 16px;padding:13px 16px;
border-top:1px solid var(--line)}
.row:first-child{border-top:0}
.label{display:flex;align-items:center;gap:10px}
.label .dot{margin-left:auto}
.ricon{width:30px;height:30px;border-radius:9px;display:grid;place-items:center;flex:none;
background:var(--soft);color:var(--accent)}
.ricon svg{width:17px;height:17px}
.row>div:last-child{padding-top:4px}
.bar{height:8px;background:var(--soft);border-radius:4px;overflow:hidden;margin-top:6px;max-width:440px}
.bar i{display:block;height:100%%;background:linear-gradient(90deg,var(--b1),var(--b2))}
.bar.warn i{background:var(--warn)}
.more>summary{margin:0;list-style:none}
.more>summary::-webkit-details-marker{display:none}
.more>summary .chev{display:inline-block;margin-left:8px;vertical-align:-1px}
.more[open]>summary .chev{transform:rotate(90deg)}
.meters{display:grid;grid-template-columns:62px minmax(0,440px) 132px;gap:12px 14px;margin-top:10px}
.meter{grid-column:1/-1;display:grid;grid-template-columns:subgrid;row-gap:3px;align-items:center;
grid-template-areas:"k bar sp" "k val sp"}
.meter.nobar{grid-template-areas:"k val sp"}
.meter>span{grid-area:k;font-size:13px;color:var(--muted)}
.meter .bar{grid-area:bar;margin:0;max-width:none;height:10px;display:flex}
.meter .bar i{flex:none}
.meter .bar i.cache{background:color-mix(in srgb,var(--b2) 28%%,transparent)}
.meter .val{grid-area:val;display:flex;align-items:baseline;gap:8px;min-width:0;font-size:13px}
.meter .val b{white-space:nowrap}
.meter .val small{margin-left:auto;min-width:0;color:var(--muted);font-size:12px;white-space:nowrap;
overflow:hidden;text-overflow:ellipsis}
.meter .sp{grid-area:sp}
.spark{display:block;width:132px;height:30px;color:var(--b2);background:var(--soft);border-radius:4px}
.spark.warn{color:var(--warn)}
.sp small{display:block;font-size:11px;color:var(--muted);text-align:right}
.sp .none{font-size:11px;color:var(--muted);text-align:right}
ul.details{margin:4px 0 0;padding-left:18px;color:var(--muted);font-size:13px}
a.tile:hover{position:relative;z-index:2}
.flags{display:flex;align-items:center;gap:6px;margin-left:auto}
.alert{position:relative;display:grid;cursor:help}
.alert svg{width:19px;height:19px}
.alert.own{color:var(--down)}.alert.upstream{color:var(--warn)}
.tip{display:none;position:absolute;z-index:3;top:calc(100%% + 6px);right:-6px;width:max-content;
max-width:min(300px,80vw);padding:10px 12px;border-radius:10px;font-size:13px;font-weight:400;
color:var(--fg);background:var(--card);border:1px solid var(--line);
box-shadow:0 8px 28px rgba(0,0,0,.28)}
.tip b{display:block;margin-bottom:4px;font-size:12px;color:var(--muted)}
.tip ul{margin:0;padding-left:16px}
.alert:hover .tip,.alert.open .tip{display:block}
code{font-size:13px}
a{color:var(--accent)}
h1{font-size:24px;margin:8px 0 12px}
ol.steps{padding:0;list-style:none}
ol.steps li{display:flex;gap:12px;align-items:flex-start;padding:10px 0;border-top:1px solid var(--line)}
ol.steps li>.dot{margin-top:6px}
pre{font-size:12px;overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px}
table.svc{width:100%%;border-collapse:collapse;background:var(--card);border:1px solid var(--line)}
table.svc th,table.svc td{padding:10px 12px;text-align:left;border-top:1px solid var(--line);font-size:14px}
table.svc th{font-size:12px;color:var(--muted);border-top:0}
form.code{display:flex;gap:8px;margin-top:8px}
form.code input{font:inherit;width:9em;padding:6px 10px;letter-spacing:.1em;color:inherit;
background:var(--bg);border:1px solid var(--line);border-radius:8px}
form.code button{font:inherit;font-weight:600;padding:6px 16px;color:#fff;border:0;border-radius:8px;
cursor:pointer;background:linear-gradient(90deg,var(--b1),var(--b2))}
form.code button:disabled{opacity:.6}
summary{cursor:pointer;margin-top:6px}
footer{margin-top:30px;color:var(--muted);font-size:12px;display:flex;justify-content:space-between;
flex-wrap:wrap;gap:6px}
@media (max-width:600px){.meters{grid-template-columns:minmax(0,1fr);gap:12px}.meter{grid-template-columns:56px minmax(0,1fr) 96px;column-gap:10px;grid-template-areas:"k val val" "bar bar sp";align-items:center}.meter>span{align-self:baseline}.meter.nobar{grid-template-areas:"k val sp"}.spark{width:96px;height:24px}.sp small{display:none}.row{grid-template-columns:1fr}.row>div:last-child{padding-top:0}
.word{font-size:22px}}
@media (max-width:380px){.meter{grid-template-columns:52px minmax(0,1fr) 80px;column-gap:8px}.spark{width:80px}}
.many .tile{flex-direction:column;gap:10px;padding:14px}
@media (max-width:860px){.tile{flex-direction:column;gap:10px;padding:14px}.tile b{font-size:16px}}
@media (max-width:640px){
.groups{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
.grp{grid-column:span 1;order:2}.grp.wide{grid-column:span 2}
.g-watch{order:0}.g-download{order:1}
.why{display:none}.folded .apps,.others .apps{grid-template-columns:1fr}}
""" % BRAND


# Tries the apps' own tailnet names from the viewer's device: a tile whose name doesn't answer
# here links to the app's port instead, and /tailscale shows which ones answer.
PROBE_SCRIPT = """
const probed={};
function probe(url){
  if(!(url in probed))probed[url]=fetch(url,{mode:"no-cors",cache:"no-store",signal:AbortSignal.timeout(5000)})
    .then(()=>true,()=>false);
  return probed[url];
}
function useReachable(){
  for(const a of document.querySelectorAll("a[data-fallback]")){
    probe(a.getAttribute("href")).then(ok=>{if(!ok)a.href=a.dataset.fallback;});
  }
  for(const d of document.querySelectorAll("[data-probe]")){
    probe(d.dataset.probe).then(ok=>{d.className="dot "+(ok?"ok":"down");d.title=ok?"reachable":"not reachable from here";});
  }
}
"""

# The footer's "Notify this device": subscribes this browser to the box's pushes (it shows
# only where that can work: HTTPS, the browser supports it, python3-cryptography is there).
PUSH_SCRIPT = """
(async()=>{
  const link=document.getElementById("push-link");
  if(!link||!("serviceWorker" in navigator)||!("PushManager" in window)||!isSecureContext)return;
  const call=(path,body)=>fetch(path,{method:"POST",headers:{"X-Requested-With":"torrent-box","Content-Type":"application/json"},body:JSON.stringify(body||{})}).then(async r=>{
    const d=await r.json().catch(()=>({}));if(!r.ok)throw new Error(d.error||("the box answered "+r.status));return d;});
  const bytes=t=>Uint8Array.from(atob(t.replace(/-/g,"+").replace(/_/g,"/").padEnd(Math.ceil(t.length/4)*4,"=")),c=>c.charCodeAt(0));
  const name=()=>{const u=navigator.userAgent;
    return (/iPhone/.test(u)?"iPhone":/iPad/.test(u)?"iPad":/Android/.test(u)?"Android":/Windows/.test(u)?"Windows":/Mac/.test(u)?"Mac":/Linux/.test(u)?"Linux":"Device")+" "+
      (/Edg\\//.test(u)?"Edge":/Firefox\\//.test(u)?"Firefox":/Chrome\\//.test(u)?"Chrome":/Safari\\//.test(u)?"Safari":"browser");};
  let info;
  try{info=await (await fetch("/push.json",{cache:"no-store"})).json();}catch(e){return;}
  if(!info.supported)return;
  const reg=await navigator.serviceWorker.register("/sw.js");
  let sub=await reg.pushManager.getSubscription();
  if(sub)call("/push/subscribe",{...sub.toJSON(),label:name()}).catch(()=>{});  // the box may have lost it
  const draw=()=>{
    link.hidden=false;
    link.textContent=Notification.permission==="denied"?"Notifications are blocked":sub?"Stop notifying this device":"Notify this device";
    link.title=sub?"Pushes to this device: click to stop":"";
  };
  link.onclick=async ev=>{
    ev.preventDefault();
    try{
      if(Notification.permission==="denied")throw new Error("Notifications are blocked for this site in the browser's settings.");
      if(sub){await call("/push/unsubscribe",{endpoint:sub.endpoint});await sub.unsubscribe();sub=null;}
      else{
        if(await Notification.requestPermission()!=="granted")throw new Error("Notifications weren't allowed.");
        sub=await reg.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:bytes(info.key)});
        await call("/push/subscribe",{...sub.toJSON(),label:name()});
        await call("/push/test").catch(()=>{});
      }
    }catch(e){alert(e.message);}
    draw();
  };
  draw();
})();
"""


SCRIPT = PROBE_SCRIPT + """
useReachable();
function unfold(){  // the folded group and the meters stay open on this browser once opened
  for(const d of document.querySelectorAll("details[data-fold]")){
    let open=false;
    try{open=localStorage.getItem("torrent-box."+d.dataset.fold)==="open";}catch(e){}
    d.open=open;
  }
}
unfold();
document.addEventListener("toggle",ev=>{
  if(!ev.target.matches?.("details[data-fold]"))return;
  try{localStorage.setItem("torrent-box."+ev.target.dataset.fold,ev.target.open?"open":"closed");}catch(e){}
},true);
function localTimes(){  // the box's times in the viewer's time zone
  for(const t of document.querySelectorAll("time[datetime]")){
    t.title=t.textContent;
    t.textContent=new Date(t.dateTime).toLocaleTimeString([],{hour:"2-digit",minute:"2-digit",second:"2-digit"});
  }
}
localTimes();
function busy(){  // don't swap the page under someone typing a code
  return document.querySelector("main details[open]:not([data-fold]), main .alert.open")||
    [...document.querySelectorAll("main input")].some(i=>i.value)||
    document.activeElement?.closest?.("main form");
}
async function refresh(){
  if(busy())return;
  try{
    const r=await fetch(location.href,{cache:"no-store"});
    const page=new DOMParser().parseFromString(await r.text(),"text/html");
    document.querySelector("main").replaceWith(page.querySelector("main"));
    unfold();
    localTimes();
    useReachable();
    document.title=page.title;
    document.querySelector("link[rel=icon]").href=page.querySelector("link[rel=icon]").href;
  }catch(e){
    document.getElementById("updated").textContent="can't reach the box, retrying";
  }
}
setInterval(refresh,%d000);
document.addEventListener("click",ev=>{  // tap an alert for its list (phones have no hover)
  const a=ev.target.closest(".alert");
  for(const o of document.querySelectorAll(".alert.open"))if(o!==a)o.classList.remove("open");
  if(a){ev.preventDefault();a.classList.toggle("open");}
});
document.addEventListener("submit",ev=>{
  const b=ev.target.querySelector("button");
  b.disabled=true;b.textContent="Logging in…";
});
""" % REFRESH_SECONDS


def upstream_lines(c):
    return [f"not its own problem: {m}" for m in c["upstream"]]


def sparkline(points, now, top, warn=False):
    """An hour of (time, percent) as a small line, 0 at the bottom and top at the top."""
    if len(points) < 2:
        return '<div class="none">the last hour shows up here</div>'
    w, h = 132, 30
    xy = [(max(0, (t - now + 3600) / 3600) * w, h - 1 - min(v, top) / top * (h - 2))
          for t, v in points]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in xy)
    area = f"M{xy[0][0]:.1f},{h} L{line.replace(' ', ' L')} L{xy[-1][0]:.1f},{h} Z"
    return (f'<svg class="spark{" warn" if warn else ""}" viewBox="0 0 {w} {h}" '
            f'preserveAspectRatio="none" aria-hidden="true">'
            f'<path d="{area}" fill="currentColor" opacity=".15"/>'
            f'<polyline points="{line}" fill="none" stroke="currentColor" stroke-width="1.5" '
            f'vector-effect="non-scaling-stroke" stroke-linejoin="round"/></svg>')


def meter(label, value, note="", bar="", spark="", caption=""):
    """One line of meters: a label, a bar (or none), the value with a note, and the last hour."""
    e = html.escape
    return (f'<div class="meter{"" if bar else " nobar"}"><span>{e(label)}</span>{bar}'
            f'<div class="val"><b>{e(value)}</b><small>{e(note)}</small></div>'
            f'<div class="sp">{spark}' + (f'<small>{e(caption)}</small>' if caption else "")
            + '</div></div>')


def bar_html(percents, warn=False, title=""):
    """A bar of one or more parts (the first solid, then the cache's lighter)."""
    parts = "".join(f'<i{" class=\"cache\"" if i else ""} style="width:{p:.1f}%"></i>'
                    for i, p in enumerate(percents))
    title = f' title="{html.escape(title)}"' if title else ""
    return f'<div class="bar{" warn" if warn else ""}"{title}>{parts}</div>'


def more_html(cid, summary, inside):
    """A row's summary that opens to show its meters; open or not is kept per browser."""
    return (f'<details class="more" data-fold="more-{cid}"><summary>{summary}'
            f'<span class="chev">›</span></summary>{inside}</details>')


def disk_io_html(c, now):
    """The disk's health, opening to its reads and writes, with their last hour."""
    e = html.escape
    summary, out = e(c["summary"]), []
    for disk in c.get("io", []):
        history = disk["history"]
        peak = max((max(r, w) for _, r, w, _ in history), default=0)
        top = max(1 << 20, peak * 1.15)  # at least 1 MB/s: idle isn't a wobbly line
        busy = ("" if disk["busy"] is None else
                " · idle" if disk["busy"] < 0.5 else f" · busy {disk['busy']:.0f}%")
        if len(c["io"]) > 1:  # each says which, and how busy
            out.append(f'<div class="muted">{e(disk["name"] + busy)}</div>')
        else:
            summary += e(busy)
        meters = []
        for i, label in ((1, "Read"), (2, "Write")):
            rate = disk[label.lower()]
            meters.append(meter(
                label, "–" if rate is None else short_speed(rate),
                f"peak {short_speed(max(h[i] for h in history))}" if len(history) > 1 else "",
                spark=sparkline([(h[0], h[i]) for h in history], now, top),
                caption="last hour" if len(history) > 1 else ""))
        out.append(f'<div class="meters">{"".join(meters)}</div>')
    return more_html("disk", summary, "".join(out)) if out else summary


def short_size(n):
    """Bytes as short as a meter needs: "1.0 GB" rather than "1011.6 MB", "863 GB"."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" or n >= 100 else f"{n:.1f} {unit}"
        n /= 1024


def size_of(part, whole):
    """"4.3 / 12.9 GB": the unit once when both have the same."""
    (n, unit), whole = short_size(part).split(), short_size(whole)
    return f"{n} / {whole}" if whole.endswith(" " + unit) else f"{n} {unit} / {whole}"


def system_html(c, now):
    """The System row's summary, opening to meters: CPU and memory with their last hour, and
    the OS disk."""
    m = c.get("metrics") or {}
    history = m.get("history", [])
    meters = []
    if "load" in m:
        cpu = m.get("cpu")
        shown = cpu if cpu is not None else min(100, m["load"] * 100 / (m["cpus"] or 1))
        peak = max((v for _, v, _ in history), default=0)
        top = max(10, peak * 1.15)  # mostly idle: a 0-100 scale would be a flat line
        meters.append(meter(
            "CPU", f"{shown:.0f}%", f"load {m['load']:.2f} · {m['cpus']} CPUs",
            bar_html([shown], shown >= 90),
            sparkline([(t, v) for t, v, _ in history], now, top, peak >= 90),
            f"last hour · peak {peak:.0f}%" if len(history) > 1 else ""))
    if "memory" in m:
        mem = m["memory"]
        total = mem["total"] or 1
        used = mem["used"] * 100 / total
        meters.append(meter(
            "Memory", f"{used:.0f}%",
            f"{size_of(mem['used'], mem['total'])} · cache {short_size(mem['cache'])}",
            bar_html([used, mem["cache"] * 100 / total], used >= 90,
                     f"apps {human_size(mem['used'])}, cache {human_size(mem['cache'])} "
                     f"(given back when apps need it), free {human_size(mem['free'])}"),
            sparkline([(t, v) for t, _, v in history], now, 100, used >= 90),
            "last hour" if len(history) > 1 else ""))
    if "disk" in m:
        d = m["disk"]
        meters.append(meter(
            "OS disk", f"{d['percent']}%",
            f"{short_size(d['free'])} free of {short_size(d['used'] + d['free'])}",
            bar_html([d["percent"]], d["percent"] >= 90)))
    summary = html.escape(c["summary"])
    return more_html("system", summary, f'<div class="meters">{"".join(meters)}</div>') \
        if meters else summary


def details_html(details):
    if not details:
        return ""
    items = "".join(f"<li>{html.escape(d)}</li>" for d in details)
    return f'<ul class="details">{items}</ul>'


ALERT_ICON = ('<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z" '
              'fill="currentColor" stroke="none"/><path d="M12 9v4M12 17h.01" stroke="#fff"/>')


def alert_html(kind, heading, items):
    """A tile's warning sign, its list in a tooltip: red for the app's own problems, yellow
    for what it reports about something else."""
    if not items:
        return ""
    lis = "".join(f"<li>{html.escape(i)}</li>" for i in items)
    return (f'<span class="alert {kind}" role="img" aria-label="{heading}: {len(items)}">'
            f'{glyph(ALERT_ICON, "currentColor")}<span class="tip"><b>{heading}</b><ul>{lis}</ul></span></span>')


def app_badge(cid, status, size=44):
    """A tile's picture: the app's own logo, or a glyph of ours in its colors."""
    if cid in status.get("logos", ()):
        return f'<img class="logo" src="/icon/{cid}" alt="" width="{size}" height="{size}">'
    colors, paths = APP_ICONS[cid]
    return (f'<span class="badge" style="background:linear-gradient(135deg,{colors})">'
            f'{glyph(paths, "#fff")}</span>')


def short_version(version):
    """"4.1.1 (56442e2929)", "3.0.2.5780" -> "v4.1.1", "v3.0.2" (the rest is on hover)."""
    return "v" + ".".join(str(version).split()[0].split(".")[:3])


def short_speed(n):
    """Bytes/s as short as a tile needs: "↓250 MB/s ↑100 MB/s" just fits."""
    for unit in ("B", "kB", "MB"):
        if n < 1000 or unit == "MB":
            return f"{n:.1f} {unit}/s" if n < 10 and unit != "B" else f"{n:.0f} {unit}/s"
        n /= 1024
    return ""


def tile_line(c):
    """A tile's last line: the app's version while it's fine (its dot says that much), or what
    is wrong; Transmission's is its speeds, or idle (the version and counts are on hover)."""
    if c["state"] != OK:
        return c["summary"]
    if "rate_down" in c:
        if not (c["rate_down"] or c["rate_up"]):
            return "idle"
        return f'↓{short_speed(c["rate_down"])} ↑{short_speed(c["rate_up"])}'
    line = short_version(c["version"]) if c.get("version") else c["summary"]
    return f'{line} · {c["build"]}' if c.get("build") else line  # Desireerr: a checkout of it


def tile_title(c):
    version = c.get("version")
    return c["summary"] + (f" · version {version}" if version and version not in c["summary"] else "")


def tile_href(cid, port, https=False, tailnet=None):
    """Plain HTTP: the app under our bar (/open/). Over HTTPS an app with its own name on the
    tailnet links there, so a phone opens its installed app; where that name doesn't answer
    (PROBE_SCRIPT), and for the other apps, under our bar too, which keeps an installed start
    page in its own window."""
    if https and (service := ((tailnet or {}).get("links") or {}).get(str(port))):
        return f'https://{html.escape(service)}/" data-fallback="/open/{cid}'
    return f"/open/{cid}"


TILE = {cid: (name, purpose, port) for cid, name, purpose, port in TILES}


def tile_html(cid, status, https, tailnet):
    e = html.escape
    name, purpose, port = TILE[cid]
    c = status["checks"][cid]
    return (f'<a class="tile" href="{tile_href(cid, port, https, tailnet)}">{app_badge(cid, status)}<div>'
            f'<div class="name"><b>{name}</b><span class="flags">'
            f'{alert_html("own", "Its own problems", c["details"])}'
            f'{alert_html("upstream", "Not its own problem", c["upstream"])}'
            f'<span class="dot {c["state"]}" title="{c["state"]}"></span></span></div>'
            f'<div class="muted">{purpose}</div><div class="line" title="{e(tile_title(c))}">'
            f'{e(tile_line(c))}</div></div></a>')


OTHER_ICON = ('<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.5 2.7 3.8 5.7 3.8 9s-1.3 6.3-3.8 9'
              'M12 3c-2.5 2.7-3.8 5.7-3.8 9s1.3 6.3 3.8 9"/>')


def other_url(entry, host, https=False):
    """Where the viewer opens another service: over HTTPS (on the tailnet) its tailnet address
    if it has one; else a port on the LAN (on the host the page was opened with, or the one
    address it listens on); else its tailnet address all the same."""
    lan = [(int(port), addr) for port, addr in entry["lan"].items()]
    lan.sort(key=lambda pa: (pa[1] != "", pa[0]))  # on all addresses first
    by_lan = [f"http://{html.escape(addr or host)}:{port}/" if ":" not in (addr or host)
              else f"http://[{html.escape((addr or host).strip('[]'))}]:{port}/" for port, addr in lan]
    order = entry["https"] + by_lan if https else by_lan + entry["https"]
    return order[0] if order else ""


def other_where(entry):
    """A service's addresses, short: "port 8200 · pantry", the tailnet ones by name."""
    names = []
    for url in entry["https"]:
        netloc = urllib.parse.urlsplit(url).netloc
        name, _, port = netloc.partition(":")
        names.append(f"tailnet :{port}" if port else name.split(".")[0])
    ports = ", ".join(str(p) for p in entry["ports"])
    return " · ".join([f"port{'s' if len(entry['ports']) > 1 else ''} {ports}"] + names)


def others_html(status, host, https):
    """Other services: the box's web services that aren't torrent-box's (other_services), with
    their own icons (served at /icon/other-<id>), else a globe of ours."""
    e = html.escape
    apps = []
    for entry in status.get("others") or ():
        if not (url := other_url(entry, host, https)):
            continue
        badge = (f'<img class="logo" src="/icon/other-{e(entry["id"])}" alt="" width="30" height="30">'
                 if entry.get("icon") else f'<span class="badge other">{glyph(OTHER_ICON, "#fff")}</span>')
        apps.append(f'<a class="app" href="{url}">{badge}<div><b>{e(entry["name"])}</b>'
                    f'<div class="line muted" title="{e(other_where(entry))}">{e(other_where(entry))}</div></div></a>')
    if not apps:
        return ""
    return f'<h2>Other services</h2><div class="others"><div class="apps">{"".join(apps)}</div></div>'


def folded_html(status, https, tailnet):
    """The last group (FOLDED): one line, closed until opened (the script remembers that), with
    whatever is wrong in it on that line."""
    e = html.escape
    checks = status["checks"]
    ids = [cid for cid in FOLDED if cid in checks]
    if not ids:
        return ""
    faces, apps, wrong = [], [], []
    for cid in ids:
        name, purpose, port = TILE[cid]
        c = checks[cid]
        badge = app_badge(cid, status, 22)
        faces.append(f'<span class="face" title="{name}">{badge}<i class="dot {c["state"]}"></i></span>')
        if c["state"] != OK:
            wrong.append(f"<span><b>{name}</b>: {e(c['summary'])}</span>")
        apps.append(
            f'<a class="app" href="{tile_href(cid, port, https, tailnet)}">{app_badge(cid, status, 30)}'
            f'<div><b>{name}</b> <span class="muted">{purpose}</span>'
            f'<div class="line muted" title="{e(tile_title(c))}">{e(tile_line(c))}</div></div>'
            f'<span class="flags">{alert_html("own", "Its own problems", c["details"])}'
            f'{alert_html("upstream", "Not its own problem", c["upstream"])}'
            f'<span class="dot {c["state"]}" title="{c["state"]}"></span></span></a>')
    if wrong:
        state = (f'<span class="wrong">{glyph(ALERT_ICON, "currentColor")}'
                 f'{" · ".join(wrong)}</span>')
    else:
        state = (f'<span class="muted">all {len(ids)} fine<span class="why"> · rarely needed once '
                 f'setup.sh has set them up</span></span>')
    return (f'<details class="folded" data-fold="folded"><summary><span class="chev">›</span><h3>{e(GROUPS[-1][0])}</h3>'
            f'<span class="faces">{"".join(faces)}</span>{state}</summary>'
            f'<div class="apps">{"".join(apps)}</div></details>')


# Transmission only lets its own origin frame its web UI: /open/transmission shows it through
# the start page, which passes /transmission/ on to it (its login and session header included).
TRANSMISSION = ("127.0.0.1", 9091)
PROXY_MAX = 32 << 20  # a .torrent file added in its web UI comes in the request
PROXY_HEADERS = ("Authorization", "X-Transmission-Session-Id", "Content-Type", "Accept",
                 "If-None-Match", "If-Modified-Since")
HOP_HEADERS = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade",
               "content-length", "date", "server"}

OPEN_CSS = """
:root{color-scheme:light dark;--bar:#fff;--fg:#1b1f2e;--muted:#646b80;--line:#e3e5ee;--hover:#eef0fb;
--ok:#1f9d55;--warn:#d98a00;--down:#d64545}
@media (prefers-color-scheme:dark){:root{--bar:#171a24;--fg:#e7e9f2;--muted:#9aa1b5;--line:#272b3a;--hover:#20243a}}
*{box-sizing:border-box}
html,body{height:100%;margin:0}
body{display:flex;flex-direction:column;background:var(--bar);color:var(--fg);
font:14px/1.3 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
nav{display:flex;align-items:center;gap:4px;height:48px;padding:0 8px;flex:none;position:relative;
background:var(--bar);border-bottom:1px solid var(--line)}
nav a,nav summary{display:flex;align-items:center;gap:7px;color:inherit;text-decoration:none;
border-radius:8px;padding:4px 6px;cursor:pointer}
nav a:hover,nav summary:hover{background:var(--hover)}
nav img,nav svg,.badge{width:26px;height:26px;flex:none}
.home svg{width:28px;height:28px}
.badge{border-radius:7px;display:grid;place-items:center}.badge svg{width:15px;height:15px}
.home b{font-weight:750;letter-spacing:-.01em}
.sep{width:1px;height:22px;background:var(--line);margin:0 4px;flex:none}
.apps{display:flex;align-items:center;gap:2px;flex:1;min-width:0}
.grp{display:flex;align-items:center;gap:1px}
.grp small,.folded small{font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);
margin-right:4px;white-space:nowrap}
.grp>a{position:relative;opacity:.6}.grp>a:hover,.grp>a.on{opacity:1}
a.on,summary.on{background:var(--hover)}
.name{display:none}.on>.name{display:inline;font-weight:600}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;flex:none;box-shadow:0 0 0 2px var(--bar)}
.dot.ok{background:var(--ok)}.dot.warn{background:var(--warn)}.dot.down{background:var(--down)}
nav .dot{position:absolute;top:2px;left:26px}nav .dot.ok{display:none}.menu .dot{position:static;box-shadow:none}
.folded{position:relative}
.folded summary{list-style:none;position:relative;color:var(--muted)}
.folded summary::-webkit-details-marker{display:none}
.fold{display:grid;grid-template-columns:1fr 1fr;gap:2px;width:26px;height:26px;padding:2px;
border-radius:7px;background:var(--hover);flex:none}
.fold img,.fold .badge,.fold svg{width:10px;height:10px;border-radius:2px}.fold .badge svg{width:7px;height:7px}
.menu{position:absolute;top:calc(100% + 8px);left:0;z-index:5;min-width:270px;padding:6px;
background:var(--bar);border:1px solid var(--line);border-radius:12px;box-shadow:0 10px 30px rgba(0,0,0,.25)}
.menu a{display:flex;align-items:center;gap:10px;padding:7px 8px}
.menu a>span{display:flex;flex-direction:column;flex:1;min-width:0}
.menu small{color:var(--muted);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.own{color:var(--muted);white-space:nowrap;margin-left:auto}
.own:hover{color:var(--fg)}
iframe{flex:1;width:100%;border:0;display:block}
@media (max-width:640px){.home b,.own span,.grp small,.folded small,summary.on>.name{display:none}
.folded{position:static}.menu{left:auto;right:8px}}
"""


def port_url(host, port, https=False, tailnet=None):
    """The app on its own port, on the host the page was opened with; over HTTPS its HTTPS port
    on the box's tailnet name (the page may be opened by its own service name, which has none
    of the apps' ports)."""
    if not https:
        return f"http://{html.escape(host)}:{port}/"
    return f"https://{html.escape((tailnet or {}).get('name') or host)}:{port + HTTPS_OFFSET}/"


def frame_src(cid, host, port, https=False, tailnet=None, at=""):
    if cid == "transmission":
        return "/transmission/web/"
    return port_url(host, port, https, tailnet) + html.escape(at[1:])


def frame_at(query):
    """?at=: where in the app the frame was (an app that says so, see OPEN_SCRIPT), a path on
    the app's own host, or "" for its start page."""
    at = urllib.parse.parse_qs(query).get("at", [""])[0]
    if not re.fullmatch(r"/(?![/\\])[^\\\x00-\x20\x7f]{0,2000}", at):
        return ""
    return at


def render_open(status, cid, host, https=False, at=""):
    """/open/<app>: the app in a frame under a bar with the logo back to the start page, the
    apps in their GROUPS to switch to (the folded group as a menu) and a link to open it on its
    own. The links go where the start page's tiles go (tile_href). AT: the page in the app to
    open (frame_at)."""
    e = html.escape
    checks = status["checks"]
    tailnet = checks.get("tailscale")
    name, _, port = TILE[cid]

    def link(other, inside, size=26):
        n, _, p = TILE[other]
        current = ' class="on" aria-current="page"' if other == cid else ""
        return (f'<a href="{tile_href(other, p, https, tailnet)}"{current} title="{e(n)}">'
                f'{app_badge(other, status, size)}{inside}'
                f'<i class="dot {checks[other]["state"]}"></i></a>')

    parts = []
    for label, ids in GROUPS[:-1]:
        links = [link(other, f'<span class="name">{e(TILE[other][0])}</span>')
                 for other in ids if other in checks]
        if links:
            parts.append(f'<div class="grp"><small>{label}</small>{"".join(links)}</div>')
    folded = [other for other in FOLDED if other in checks]
    if folded:
        worst = max((checks[o]["state"] for o in folded), key=WORST.get)
        if cid in folded:
            face = f'{app_badge(cid, status, 26)}<span class="name">{e(name)}</span>'
        else:
            face = f'<span class="fold">{"".join(app_badge(o, status, 10) for o in folded[:4])}</span>'
        items = "".join(
            link(o, f'<span><b>{e(TILE[o][0])}</b><small>{e(TILE[o][1])} · '
                    f'{e(tile_line(checks[o]))}</small></span>', 24) for o in folded)
        on = ' class="on"' if cid in folded else ""
        parts.append(
            f'<details class="folded"><summary{on} title="{e(GROUPS[-1][0])}"><small>{e(GROUPS[-1][0])}</small>{face}'
            f'<i class="dot {worst}"></i>▾</summary><div class="menu">{items}</div></details>')
    icon = f"/icon/{cid}" if cid in status.get("logos", ()) else favicon(OK)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(name)} · {e(status["hostname"])}</title>
<link rel="icon" href="{icon}">
<style>{OPEN_CSS}</style>
</head><body>
<nav><a class="home" href="/" title="Back to the start page">{logo_svg("logo")}<b>torrentbox</b></a>
<span class="sep"></span><div class="apps">{'<span class="sep"></span>'.join(parts)}</div>
<a class="own" href="{app_url(host, port, https, tailnet)}" target="_blank" rel="noopener" title="Open {e(name)} on its own">
<span>Open on its own</span> ↗</a></nav>
<iframe src="{frame_src(cid, host, port, https, tailnet, at)}" title="{e(name)}" allow="fullscreen; autoplay; clipboard-read; clipboard-write"></iframe>
<script>{OPEN_SCRIPT}</script>
</body></html>
"""


OPEN_SCRIPT = PROBE_SCRIPT + """
useReachable();
document.addEventListener("click",ev=>{  // a click elsewhere closes the menu
  for(const d of document.querySelectorAll("details.folded[open]"))if(!d.contains(ev.target))d.open=false;
});
// An app that says where it is (Desireerr does) keeps it in our address as ?at=, so a reload or
// a bookmark opens that page again; "Open on its own" goes there too.
const frame=document.querySelector("iframe"),own=document.querySelector("a.own");
addEventListener("message",ev=>{
  const at=ev.data&&ev.data.at;
  if(ev.source!==frame.contentWindow||typeof at!=="string"||!/^\\/(?![\\/\\\\])/.test(at))return;
  const url=new URL(location.href);
  if(at==="/")url.searchParams.delete("at");else url.searchParams.set("at",at);
  history.replaceState(null,"",url);
  own.href=new URL(at,own.href).href;
  if(own.dataset.fallback)own.dataset.fallback=new URL(at,own.dataset.fallback).href;
});
"""


def render(status, host, https=False):
    e = html.escape
    checks = status["checks"]
    worst = max((c["state"] for c in checks.values()), key=WORST.get, default=OK)
    names = dict((cid, name) for cid, name, *_ in TILES + ROWS)
    problems = [(cid, c) for cid, c in checks.items() if c["state"] != OK]

    if problems:
        n = len(problems)
        headline = f"{n} thing{'s' if n > 1 else ''} need{'' if n > 1 else 's'} a look"
        items = "".join(f"<li><b>{e(names.get(cid, cid))}</b>: {e(c['summary'])}</li>"
                        for cid, c in problems)
        banner = f'<div class="banner {worst}"><b>{headline}</b><ul>{items}</ul></div>'
    else:
        headline, banner = "All systems go", ""
    update = status.get("update")
    if update and update.get("commits"):
        n = update["commits"]
        items = "".join(f"<li>{e(line)}</li>" for line in update["subjects"])
        if n > len(update["subjects"]):
            items += f"<li>… and {n - len(update['subjects'])} more</li>"
        banner += (f'<div class="banner update"><b>Update available: torrent-box '
                   f'{e(update["version"])}</b> <span class="muted">({n} new commit'
                   f'{"s" if n > 1 else ""}; you have {e(version_text() or "?")})</span><ul>{items}</ul>'
                   f'<div class="muted">To update, on the box: <code>{e(update_command())}</code>'
                   f' · <a href="{e(update["url"])}">what changed</a></div></div>')

    tailnet = checks.get("tailscale")
    shown = [cid for cid, *_ in TILES if cid in checks]
    groups = []
    for label, ids in GROUPS[:-1]:
        cards = [tile_html(cid, status, https, tailnet) for cid in ids if cid in shown]
        if cards:
            groups.append(f'<section class="grp g-{label.lower()}{" wide" if len(cards) > 1 else ""}" '
                          f'style="--n:{len(cards)}"><h3>{label}</h3>{"".join(cards)}</section>')
    main_count = sum(cid not in FOLDED for cid in shown)
    folded = folded_html(status, https, tailnet)

    rows = []
    for cid, name in ROWS:
        if cid not in checks:
            continue
        c = checks[cid]
        extra, body = "", e(c["summary"])
        if cid == "system":
            body = system_html(c, status["time"])
        elif cid == "disk":
            body = disk_io_html(c, status["time"])
        elif cid == "storage":
            if "percent" in c:
                extra = (f'<div class="bar {c["state"]}"><i style="width:{c["percent"]}%"></i></div>'
                         f'<div class="muted">{e(status["storage_root"])}</div>')
        elif cid == "samba" and c["state"] == OK:
            extra = (f'<div class="muted"><code>\\\\{e(host)}\\storage</code> (Windows), '
                     f'<a href="smb://{e(host)}/storage">smb://{e(host)}/storage</a> '
                     f'(Mac, Linux, phones)</div>')
        elif cid == "ntfy" and c.get("server"):
            box = (checks.get("tailscale") or {}).get("name") if https else None  # see app_url
            url = e(ntfy_link(c["server"], box or host, https))
            extra = (f'<div class="muted"><a href="{url}/">{url}</a> '
                     f'(web app; also the server for the ntfy phone app)</div>')
        elif cid == "ncore":
            # only while a code is needed (setup_helper.py ncore-code works any time)
            if c.get("needs_code"):
                extra = ('<form class="code" method="post" action="/ncore-code">'
                         '<input name="code" inputmode="numeric" autocomplete="one-time-code" '
                         'pattern="[0-9]{6,8}" maxlength="8" required placeholder="2FA code" '
                         'aria-label="nCore 2FA code"><button>Log in</button></form>')
        rows.append(
            f'<div class="row" id="{cid}"><div class="label">'
            f'<span class="ricon">{glyph(ROW_ICONS[cid], "currentColor")}</span><b>{name}</b>'
            f'<span class="dot {c["state"]}" title="{c["state"]}"></span></div>'
            f'<div>{body}{extra}{details_html(c["details"] + upstream_lines(c))}</div></div>')

    hostname = e(status["hostname"])
    # The box's clock is often UTC: the script shows it in the viewer's time zone, and without
    # it the text says which zone it is.
    updated = (f'<time datetime="{time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(status["time"]))}">'
               f'{time.strftime("%H:%M:%S %Z", time.localtime(status["time"]))}</time>')
    title = f"{hostname} · torrent-box" if worst == OK else f"⚠ {hostname} · torrent-box"
    tailnet_link = ' · <a href="/tailscale">tailnet names</a>' if "tailscale" in checks else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="{BRAND[0]}">
<title>{title}</title>
<link rel="icon" href="{favicon(worst)}">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="apple-touch-icon" href="/app/icon-180.png">
<style>{CSS}</style>
<noscript><meta http-equiv="refresh" content="{REFRESH_SECONDS}"></noscript>
</head><body><div class="wrap">
<main>
<header class="top">
<div class="brand">{logo_svg("logo")}<div><div class="word">torrent<span>box</span></div>
<div class="host">{hostname}</div></div></div>
<div class="pill"><span class="dot {worst}"></span>{headline}</div>
</header>
{banner}
<h2>Apps</h2>
{services_note(checks, https)}
<div class="groups{" many" if main_count > 4 else ""}" style="--cols:{max(main_count, 3)}">{"".join(groups)}</div>
{folded}
{others_html(status, host, https)}
<h2>Health</h2>
<div class="rows">{"".join(rows)}</div>
<footer><span><span id="updated">Updated {updated}, refreshes every {REFRESH_SECONDS} s.</span> <a href="#" id="push-link" hidden></a></span>
<span>torrent-box{" " + e(version_text()) if VERSION else ""}{tailnet_link} · <a href="https://github.com/{GITHUB_REPO}">GitHub</a></span></footer>
</main>
</div><script>{SCRIPT}{PUSH_SCRIPT}</script></body></html>
"""


def render_tailscale(status, https=False):
    """/tailscale: the steps that give the apps their own names on the tailnet (SERVICES), each
    with its state; the script fills in which ones this device reaches."""
    e = html.escape
    checks = status["checks"]
    tailnet = checks.get("tailscale")

    def dot(ok, title=""):
        state = {True: OK, False: DOWN, None: ""}[ok]
        return f'<span class="dot {state}" title="{e(title)}"></span>'
    if not tailnet:
        body = "<p>Tailscale isn't set up on this box (<code>[setup] tailscale</code>).</p>"
    elif tailnet["state"] != OK:
        body = f"<p>Tailscale isn't connected: {e(tailnet['summary'])}.</p>"
    else:
        domain = tailnet["name"].partition(".")[2]
        wanted = [name for name, _ in wanted_services(checks)]
        approved, served = set(tailnet.get("approved", ())), tailnet.get("served", {})
        tags = tailnet.get("tags") or []
        box_tag = tags[0] if tags else "tag:server"
        grants = [{"src": ["autogroup:admin"], "dst": [f"svc:{n}" for n in wanted], "ip": ["443"]}]
        if "seerr" in wanted:  # the one app other members of the tailnet (family) may use
            grants.append({"src": ["autogroup:member"], "dst": ["svc:seerr"], "ip": ["443"]})
        policy = "\n".join([
            f'"tagOwners": {json.dumps({box_tag: ["autogroup:admin"], "tag:torrent-box": ["autogroup:admin"]})},',
            f'"autoApprovers": {json.dumps({"services": {"tag:torrent-box": [box_tag]}})},',
            '"grants": [',
            ",\n".join(f"  {json.dumps(g)}" for g in grants),
            "]"])
        rows = []
        for name in wanted:
            address = f"https://{name}.{domain}/"
            ready = name in served and name in approved
            reach = (f'<span class="dot" data-probe="{e(address)}" title="checking"></span>'
                     if ready and https else dot(None, "once it's served and approved"))
            rows.append(f'<tr><td><b>{e(name)}</b><div class="muted">{e(address)}</div></td>'
                        f'<td>{dot(name in served, "served" if name in served else "not served yet: step 5")}</td>'
                        f'<td>{dot(name in approved, "approved" if name in approved else "not defined or not approved: steps 2 and 4")}</td>'
                        f'<td>{reach}</td></tr>')
        now = f" (now: {e(', '.join(tags))})" if tags else ""
        body = f"""<p>Phones tell installed web apps apart by their name, not their port. Under their own
names on the tailnet (<a href="https://tailscale.com/kb/1552/tailscale-services">Tailscale Services</a>),
the start page and the apps install as separate apps, and the start page's links open them. Until an
app's name is ready, its tile links to its HTTPS port on <code>{e(tailnet["name"])}</code>.</p>
<h2>Steps</h2>
<ol class="steps">
<li>{dot(tailnet.get("certs"), "on" if tailnet.get("certs") else "off")}<div><b>Admin console → DNS</b>: MagicDNS and HTTPS Certificates on.</div></li>
<li>{dot(None, "shown by Approved and Reachable below")}<div><b>Admin console → Access controls</b>: a tag for the box, approval of
its services, and access to them: admins get all of them, other members only Seerr (merge with
what's there):<pre>{e(policy)}</pre></div></li>
<li>{dot(bool(tags), ", ".join(tags) or "no tag")}<div><b>Admin console → Machines</b>: this box → Edit ACL tags → <code>{e(box_tag)}</code>{now}.
A tagged device belongs to the tag: rules for your own devices (<code>autogroup:self</code>) no longer
cover it. Check that its key expiry stays off.</div></li>
<li>{dot(all(n in approved for n in wanted))}<div><b>Admin console → Services</b> → Advertise → Define a Service, for each
name below: port <code>tcp:443</code>, service tag <code>tag:torrent-box</code>.</div></li>
<li>{dot(all(n in served for n in wanted))}<div><b>On the box</b>: <code>{e(update_command().partition(" && ")[0])} &amp;&amp; ./setup.sh</code>
serves them, once the box has its tag.</div></li>
</ol>
<h2>The names</h2>
<table class="svc"><tr><th></th><th>Served</th><th>Approved</th><th>Reachable here</th></tr>{"".join(rows)}</table>
<p class="muted">Served and approved but not reachable from here: the access rule (step 2) doesn't let this
device in, or it isn't a member of the tailnet.</p>"""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="{BRAND[0]}">
<title>Tailnet names · torrent-box</title>
<link rel="icon" href="{favicon(OK)}">
<style>{CSS}</style>
</head><body><div class="wrap">
<main>
<p><a href="/">← {e(status["hostname"])}</a></p>
<h1>Own names on the tailnet</h1>
{body}
</main>
</div><script>{PROBE_SCRIPT}useReachable();</script></body></html>
"""


### Server

class Status:
    """The latest round of checks, shared by all requests for CACHE_SECONDS."""

    def __init__(self, config, arr_dir, state_dir=None):
        self.config, self.arr_dir = config, arr_dir
        self.updates = Updates(state_dir)
        self.pool = ThreadPoolExecutor(max_workers=12)
        self.lock = threading.Lock()
        self.latest, self.at = None, 0.0
        self.login_lock = threading.Lock()
        self.tries = []  # times of recent 2FA logins
        self.last_login = None  # {"time", "ok", "message"}: shown on the page for a while
        self.logos = Logos()
        self.others, self.others_job, self.others_at = None, None, 0.0
        self.other_icons = {}  # "other-<id>" -> (bytes, content type), for /icon/

    def get(self):
        with self.lock:
            if self.latest is None or time.monotonic() - self.at > CACHE_SECONDS:
                latest = gather(load_settings(self.config), self.arr_dir, self.pool,
                                self.last_login)
                latest["logos"] = self.logos.available(
                    [cid for cid, *_ in TILES if cid in latest["checks"]], self.pool)
                latest["others"] = self.other_services(latest["checks"])
                self.latest, self.at = latest, time.monotonic()
            self.latest["update"] = self.updates.info
            return self.latest

    def other_services(self, checks):
        """other_services(), looked for every OTHERS_EVERY in the background: the last ones
        found (the first time, waiting up to OTHERS_WAIT)."""
        job = self.others_job
        if job is None or (job.done() and time.monotonic() - self.others_at > OTHERS_EVERY):
            job = self.others_job = self.pool.submit(other_services, known_ports(checks))
            self.others_at = time.monotonic()
        if job.done() or self.others is None:
            try:
                found = job.result(timeout=OTHERS_WAIT)
                # the icons are served at /icon/other-<id>; status.json only says there is one
                self.other_icons = {f"other-{o['id']}": o["picture"] for o in found if o.get("picture")}
                self.others = [{**{k: v for k, v in o.items() if k != "picture"},
                                "icon": bool(o.get("picture"))} for o in found]
            except Exception as e:  # TimeoutError: the next request has them
                if not isinstance(e, TimeoutError):
                    print(f"other services: {e!r}", file=sys.stderr, flush=True)
                self.others = self.others or []
        return self.others

    def watch(self):
        """Forever: push what stays wrong (and when it's fine again)."""
        watcher = Watcher()
        while True:
            time.sleep(WATCH_EVERY)
            try:
                settings = load_settings(self.config)
                for kind, cid, c in watcher.update(self.get()["checks"], time.time()):
                    if not push_event(settings, kind, cid, c) and kind == "down":
                        watcher.retry(cid)
                self.updates.poll(settings, time.time())
            except Exception as e:  # keep watching whatever happens
                print(f"watch: {e!r}", file=sys.stderr, flush=True)

    def login(self, code):
        """Log Prowlarr in to nCore with a 2FA code; one at a time, LOGIN_TRIES per
        LOGIN_WINDOW. Returns (ok, message); the page shows it too."""
        if not self.login_lock.acquire(blocking=False):
            return False, "another login is in progress"
        try:
            now = time.time()
            self.tries = [t for t in self.tries if now - t < LOGIN_WINDOW]
            if not CODE.fullmatch(code):
                ok, message = False, "a 2FA code is 6 digits"
            elif len(self.tries) >= LOGIN_TRIES:
                ok, message = False, f"{LOGIN_TRIES} tries in {LOGIN_WINDOW // 60} minutes, wait a little"
            else:
                self.tries.append(now)
                since = outage_start(self.arr_dir)
                try:
                    sync = ncore_login(self.arr_dir, code)
                    ok, message = True, ""
                except LoginError as e:
                    ok, message = False, str(e)
            self.last_login = {"time": time.time(), "ok": ok, "message": message}
            if ok:
                self.last_login["searched"] = None
                threading.Thread(target=self.research, args=(self.last_login, since, sync),
                                 daemon=True).start()
            with self.lock:
                self.latest = None  # show it right away
            return ok, message
        finally:
            self.login_lock.release()

    def research(self, login, since, sync):
        try:
            searched = research(self.arr_dir, since, sync)
        except Exception as e:  # a thread of its own: say so rather than die quietly
            searched = [f"failed: {e!r}"]
        print(f"after the nCore login, searched again: {searched}", file=sys.stderr, flush=True)
        login["searched"] = searched
        with self.lock:
            self.latest = None


class Handler(BaseHTTPRequestHandler):
    server_version = "torrent-box-dashboard"
    status = None  # set in main()
    webpush = None  # set in main()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            body = render(self.status.get(), link_host(self.headers.get("Host"), socket.gethostname()),
                          https=self.headers.get("X-Forwarded-Proto") == "https").encode()
            kind = "text/html; charset=utf-8"
        elif path == "/status.json":
            body = json.dumps(self.status.get(), indent=1).encode()
            kind = "application/json"
        elif path.startswith("/icon/") and (logo := self.status.other_icons.get(path[6:])
                                            or self.status.logos.get(path[6:])):
            body, kind = logo
        elif path == "/tailscale":
            body = render_tailscale(self.status.get(), https=self.headers.get("X-Forwarded-Proto") == "https").encode()
            kind = "text/html; charset=utf-8"
        elif path.startswith("/open/") and path[6:] in self.status.get()["checks"] \
                and any(t[0] == path[6:] for t in TILES):
            body = render_open(self.status.get(), path[6:],
                               link_host(self.headers.get("Host"), socket.gethostname()),
                               https=self.headers.get("X-Forwarded-Proto") == "https",
                               at=frame_at(self.path.partition("?")[2])).encode()
            kind = "text/html; charset=utf-8"
        elif path == "/transmission" or path.startswith("/transmission/"):
            self.proxy_transmission()
            return
        elif path == "/manifest.webmanifest":
            body, kind = MANIFEST, "application/manifest+json"
        elif path == "/sw.js":
            body, kind = SERVICE_WORKER, "text/javascript"
        elif path == "/push.json":
            push = self.webpush
            body = json.dumps({"supported": push.supported, "devices": len(push.subs),
                               "key": push.public_key() if push.supported else ""}).encode()
            kind = "application/json"
        elif path.startswith("/app/") and (icon := webapp_icon(path[5:])):
            body, kind = icon, "image/png"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        if path.startswith(("/icon/", "/app/", "/manifest")):
            self.send_header("Cache-Control", "max-age=86400")
            # an SVG opened on its own is a document: no scripts from it on our page's origin
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
        else:
            self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def proxy_transmission(self, body=None):
        headers = {k: v for k in PROXY_HEADERS if (v := self.headers.get(k)) is not None}
        conn = http.client.HTTPConnection(*TRANSMISSION, timeout=60)
        try:
            conn.request(self.command, self.path, body, headers)
            resp = conn.getresponse()
            data = resp.read()
        except OSError as e:
            self.send_error(502, f"Transmission doesn't answer: {problem(e)}")
            return
        finally:
            conn.close()
        self.send_response(resp.status, resp.reason)
        for k, v in resp.getheaders():
            if k.lower() not in HOP_HEADERS:
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path.startswith("/transmission/"):
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if not 0 <= length <= PROXY_MAX:
                self.send_error(413 if length > PROXY_MAX else 400)
                return
            self.proxy_transmission(self.rfile.read(length))
            return
        if self.path in ("/push/subscribe", "/push/unsubscribe", "/push/test", "/push/notify"):
            self.push_post(self.path)
            return
        if self.path != "/ncore-code" or not load_settings(self.status.config)["arr"]:
            self.send_error(404)
            return
        # Browsers send Origin with a form: another site's page mustn't post codes here
        origin = self.headers.get("Origin")
        if origin is not None and urllib.parse.urlsplit(origin).netloc != self.headers.get("Host"):
            self.send_error(403, "Cross-site request")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= 1024:
            self.send_error(400)
            return
        form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        ok, message = self.status.login(re.sub(r"\s", "", form.get("code", [""])[0]))
        print(f"nCore 2FA code from {self.client_address[0]}: "
              + ("logged in" if ok else f"failed: {message}"), file=sys.stderr, flush=True)
        self.send_response(303)
        self.send_header("Location", "/#ncore")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def push_post(self, path):
        """The page's calls about push (subscribe, unsubscribe, test), and /push/notify for
        the root jobs, which prove themselves with the token in state_dir."""
        push = self.webpush
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= 4096:
            self.send_error(400)
            return
        raw = self.rfile.read(length)
        if path == "/push/notify":
            given = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
            if not push.token or not hmac.compare_digest(given.encode(), push.token.encode()):
                self.send_error(403)
                return
        elif self.headers.get(PUSH_HEADER) != "torrent-box":
            self.send_error(403, "Cross-site request")
            return
        try:
            body = json.loads(raw or b"{}")
            if not isinstance(body, dict):
                raise ValueError("not an object")
            if not push.supported:
                raise ValueError("the box needs python3-cryptography for push to devices")
            if path == "/push/subscribe":
                push.subscribe(body)
            elif path == "/push/unsubscribe":
                push.unsubscribe(str(body.get("endpoint") or ""))
            elif path == "/push/test":
                push.test()
            else:
                push.send(str(body.get("title") or "torrent-box"), str(body.get("message") or ""),
                          str(body.get("tag") or ""))
            status, out = 200, {"devices": len(push.subs)}
        except ValueError as e:
            status, out = 400, {"error": str(e)}
        data = json.dumps(out).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):  # no access log in the journal
        pass


class Server(ThreadingHTTPServer):
    daemon_threads = True


class DualStackServer(Server):
    """IPv6 and IPv4 on one socket."""
    address_family = socket.AF_INET6

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", type=int, default=80)
    parser.add_argument("-c", "--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--arr-dir", type=Path, default=DEFAULT_ARR_DIR)
    parser.add_argument("--once", action="store_true", help="print the status as JSON and exit")
    args = parser.parse_args(argv)

    # StateDirectory= in the unit (what was pushed about updates); run by hand: none
    global WEBPUSH
    Handler.status = Status(args.config, args.arr_dir, os.environ.get("STATE_DIRECTORY"))
    WEBPUSH = Handler.webpush = WebPush(os.environ.get("STATE_DIRECTORY"))
    if args.once:
        print(json.dumps(Handler.status.get(), indent=1))
        return 0
    try:
        server = DualStackServer(("::", args.port), Handler)
    except OSError as e:
        if e.errno != errno.EAFNOSUPPORT:  # e.g. the port is taken
            raise
        server = Server(("0.0.0.0", args.port), Handler)  # no IPv6 on this box
    threading.Thread(target=Handler.status.watch, daemon=True).start()
    print(f"serving on port {args.port}", file=sys.stderr, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    sys.exit(main())
