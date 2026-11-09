#!/usr/bin/env python3
"""torrent-box dashboard: the box's start page, with links to its web apps and their health.

Served on port 80 by torrent-box-dashboard.service (installed by setup.sh), so the box's name
alone (http://<box>/) opens it, on the LAN and through Tailscale. Links use the name the page
was opened with. Reads the same config.ini/secrets.ini as rss2transmission and the apps'
API keys, and never shows a secret. GET /status.json returns the same data as JSON.
With Prowlarr, POST /ncore-code takes a fresh nCore 2FA code (the page shows a form for it
while one is needed).
It also watches: whatever stays wrong for ALERT_AFTER is pushed to the ntfy topic, and
pushed again once it's fine.

Standard library only; one round of checks serves every request for a few seconds.
"""

import argparse
import base64
import calendar
import configparser
import errno
import html
import json
import os
import re
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

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

# The apps' tiles: (check id, name, what it's for, port). Shown when the check exists.
TILES = [
    ("jellyfin", "Jellyfin", "Watch movies and series", 8096),
    ("minidlna", "MiniDLNA", "DLNA server status", 8200),
    ("seerr", "Seerr", "Request movies and series", 5055),
    ("transmission", "Transmission", "Torrents", 9091),
    ("sonarr", "Sonarr", "Series", 8989),
    ("radarr", "Radarr", "Movies", 7878),
    ("whisparr", "Whisparr", "Adult", 6969),
    ("desireerr", "Desireerr", "Request adult content", 5056),
    ("prowlarr", "Prowlarr", "nCore search", 9696),
]
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

def result(state, summary, details=(), **extra):
    return {"state": state, "summary": summary, "details": list(details), **extra}


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
    parts = [f"{downloading} downloading", f"{seeding} seeding"]
    if down or up:
        parts.append(f"↓ {human_size(down)}/s ↑ {human_size(up)}/s")
    # local errors (3) are ours to fix (disk, permissions); tracker ones (1, 2) are nCore's
    broken = [t for t in torrents if t["error"] == 3]
    details = [f"{t['name']}: {t['errorString']}" for t in broken[:5]]
    if len(broken) > 5:
        details.append(f"… and {len(broken) - 5} more")
    tracker = sum(t["error"] in (1, 2) for t in torrents)
    if tracker:
        details.append(f"{tracker} with a tracker warning")
    return result(WARN if broken else OK, ", ".join(parts), details)


def check_jellyfin(s):
    state, summary = service_check("jellyfin")
    if state != OK:
        return result(state, summary)
    try:
        health = http_get("http://127.0.0.1:8096/health").decode().strip()
    except OSError as e:
        return result(WARN, f"running, but {problem(e)}")
    return result(OK if health == "Healthy" else WARN, health.lower() or "no answer")


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


def ntfy_link(server, host):
    """The ntfy server's address as the browser can reach it: "localhost" in the config is
    this box, which the viewer knows as host."""
    parts = urllib.parse.urlsplit(server)
    if parts.hostname not in ("localhost", "127.0.0.1", "::1"):
        return server
    return parts._replace(netloc=f"{host}:{parts.port}" if parts.port else host).geturl()


def api_key(config_xml):
    try:
        return ET.parse(config_xml).getroot().findtext("ApiKey") or None
    except (OSError, ET.ParseError):
        return None


def check_arr(name, arr_dir):
    port, version = ARR_APPS[name]
    key = api_key(Path(arr_dir) / name / "config.xml")
    try:
        if not key:  # can't read its health without it; is it up at all?
            http_get(f"http://127.0.0.1:{port}/ping")
            return result(OK, "running")
        issues = json.loads(http_get(f"http://127.0.0.1:{port}/api/{version}/health",
                                     {"X-Api-Key": key}))
    except (OSError, ValueError) as e:
        return result(DOWN, problem(e))
    issues = [i for i in issues if i.get("type") in ("warning", "error")]
    elsewhere = [i.get("message", "?") for i in issues if upstream_issue(name, i)]
    issues = [i.get("message", "?") for i in issues if not upstream_issue(name, i)]
    if not issues:
        return result(OK, "healthy", [f"not its own problem: {m}" for m in elsewhere])
    return result(WARN, f"{len(issues)} issue{'s' if len(issues) > 1 else ''}",
                  issues + [f"not its own problem: {m}" for m in elsewhere])


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
    prowlarr_call(arr_dir, "POST", "command", {"name": "ApplicationIndexerSync", "forceSync": True})


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
                       "requests and everything else work"])
    seerr_version = status.get("version", "?")
    return result(OK, f"running, version {seerr_version}")


def check_desireerr(s):
    try:
        status = json.loads(http_get("http://127.0.0.1:5056/api/status"))
    except (OSError, ValueError) as e:
        return result(DOWN, problem(e))
    if status.get("setup_needed"):
        return result(WARN, "waiting for its first sign-in",
                      ["the setup code is in ~/.local/state/desireerr/setup-code"])
    return result(OK, f"running, version {status.get('version', '?')}")


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
    return result(OK, f"connected as {name} ({ipv4})" if name else "connected", name=name)


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
    parts, details, state = [], [], OK
    for log in logs:
        model = log.name[len("attrlog."):-len(".ata.csv")].rsplit("-", 1)[0]  # minus serial
        try:
            attrs = read_attrlog(log)
        except OSError:
            continue
        temp = attrs.get(194, attrs.get(190))
        part = model
        if temp is not None:
            temp &= 0xFF  # some disks pack min/max into the raw value
            part += f" {temp} °C"
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
    return result(state, "; ".join(parts) or "smartd is watching", details)


def cpu_temperature():
    """CPU temperature in °C from hwmon (AMD, Intel, Raspberry Pi), or None."""
    for hwmon in sorted(Path("/sys/class/hwmon").glob("hwmon*")):
        try:
            if (hwmon / "name").read_text().strip() in ("k10temp", "coretemp", "cpu_thermal"):
                return int((hwmon / "temp1_input").read_text()) / 1000
        except (OSError, ValueError):
            continue
    return None


def check_system(s):
    parts, state = [], OK
    try:
        uptime = float(Path("/proc/uptime").read_text().split()[0])
        load = Path("/proc/loadavg").read_text().split()[0]
        mem = dict(re.findall(r"^(\w+):\s+(\d+)", Path("/proc/meminfo").read_text(), re.M))
        total, available = int(mem["MemTotal"]) * 1024, int(mem["MemAvailable"]) * 1024
        parts += [f"up {duration(uptime)}", f"load {load} ({os.cpu_count()} CPUs)",
                  f"memory {human_size(total - available)} of {human_size(total)}"]
    except (OSError, ValueError, KeyError, IndexError):
        pass
    temp = cpu_temperature()
    if temp is not None:
        parts.append(f"CPU {temp:.0f} °C")
    details = []
    used, free, percent = disk_usage("/")
    if percent >= 90:
        state = WARN
        details.append(f"system disk {percent}% full ({human_size(free)} free)")
    return result(state, ", ".join(parts) or "?", details)


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
            checks["desireerr"] = check_desireerr
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
    if not s["ntfy_topic"]:
        return True
    name = dict((i, n) for i, n, *_ in TILES + ROWS).get(cid, cid)
    if kind == "down":
        title = f"{name} is down" if c["state"] == DOWN else f"{name} needs a look"
        message = "\n".join([c["summary"], *c["details"]])
        priority, tags = s["priority_problem"], ["rotating_light" if c["state"] == DOWN else "warning"]
    else:
        title, message, priority, tags = f"{name} is fine again", c["summary"], 3, ["white_check_mark"]
    body = {"topic": s["ntfy_topic"], "title": title, "message": message, "priority": priority,
            "tags": tags, "click": f"http://{socket.gethostname()}/"}
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


def link_host(header, fallback):
    """The host the browser used (so links work by IP, name or Tailscale name), if sane."""
    match = HOST.fullmatch((header or "").strip())
    return match["host"] if match else fallback


# torrent-box's mark: a box with a download arrow, in the brand gradient
BRAND = ("#4f5bd5", "#a14fd6")


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
.banner ul{margin:6px 0 0;padding-left:20px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:12px}
.tile{display:flex;gap:14px;align-items:flex-start;padding:16px;background:var(--card);
border:1px solid var(--line);border-radius:14px;color:inherit;text-decoration:none;
box-shadow:var(--shadow);transition:transform .12s,border-color .12s}
a.tile:hover{border-color:var(--accent);transform:translateY(-2px)}
.badge{width:44px;height:44px;border-radius:12px;display:grid;place-items:center;flex:none}
.badge svg{width:24px;height:24px}
.logo{width:44px;height:44px;flex:none;object-fit:contain}
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
ul.details{margin:4px 0 0;padding-left:18px;color:var(--muted);font-size:13px}
code{font-size:13px}
a{color:var(--accent)}
form.code{display:flex;gap:8px;margin-top:8px}
form.code input{font:inherit;width:9em;padding:6px 10px;letter-spacing:.1em;color:inherit;
background:var(--bg);border:1px solid var(--line);border-radius:8px}
form.code button{font:inherit;font-weight:600;padding:6px 16px;color:#fff;border:0;border-radius:8px;
cursor:pointer;background:linear-gradient(90deg,var(--b1),var(--b2))}
form.code button:disabled{opacity:.6}
summary{cursor:pointer;margin-top:6px}
footer{margin-top:30px;color:var(--muted);font-size:12px;display:flex;justify-content:space-between;
flex-wrap:wrap;gap:6px}
@media (max-width:600px){.row{grid-template-columns:1fr}.row>div:last-child{padding-top:0}
.word{font-size:22px}}
""" % BRAND


SCRIPT = """
function localTimes(){  // the box's times in the viewer's time zone
  for(const t of document.querySelectorAll("time[datetime]")){
    t.title=t.textContent;
    t.textContent=new Date(t.dateTime).toLocaleTimeString([],{hour:"2-digit",minute:"2-digit",second:"2-digit"});
  }
}
localTimes();
function busy(){  // don't swap the page under someone typing a code
  return document.querySelector("main details[open]")||
    [...document.querySelectorAll("main input")].some(i=>i.value)||
    document.activeElement?.closest?.("main form");
}
async function refresh(){
  if(busy())return;
  try{
    const r=await fetch(location.href,{cache:"no-store"});
    const page=new DOMParser().parseFromString(await r.text(),"text/html");
    document.querySelector("main").replaceWith(page.querySelector("main"));
    localTimes();
    document.title=page.title;
    document.querySelector("link[rel=icon]").href=page.querySelector("link[rel=icon]").href;
  }catch(e){
    document.getElementById("updated").textContent="can't reach the box, retrying";
  }
}
setInterval(refresh,%d000);
document.addEventListener("submit",ev=>{
  const b=ev.target.querySelector("button");
  b.disabled=true;b.textContent="Logging in…";
});
""" % REFRESH_SECONDS


def details_html(details):
    if not details:
        return ""
    items = "".join(f"<li>{html.escape(d)}</li>" for d in details)
    return f'<ul class="details">{items}</ul>'


def render(status, host):
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

    tiles = []
    for cid, name, purpose, port in TILES:
        if cid not in checks:
            continue
        c = checks[cid]
        if cid in status.get("logos", ()):
            badge = f'<img class="logo" src="/icon/{cid}" alt="" width="44" height="44">'
        else:
            colors, paths = APP_ICONS[cid]
            badge = (f'<span class="badge" style="background:linear-gradient(135deg,{colors})">'
                     f'{glyph(paths, "#fff")}</span>')
        tiles.append(
            f'<a class="tile" href="http://{e(host)}:{port}/">{badge}<div>'
            f'<div class="name"><b>{name}</b><span class="dot {c["state"]}" title="{c["state"]}"></span></div>'
            f'<div class="muted">{purpose}</div><div>{e(c["summary"])}</div>'
            f'{details_html(c["details"])}</div></a>')

    rows = []
    for cid, name in ROWS:
        if cid not in checks:
            continue
        c = checks[cid]
        extra = ""
        if cid == "storage":
            if "percent" in c:
                extra = (f'<div class="bar {c["state"]}"><i style="width:{c["percent"]}%"></i></div>'
                         f'<div class="muted">{e(status["storage_root"])}</div>')
        elif cid == "samba" and c["state"] == OK:
            extra = (f'<div class="muted"><code>\\\\{e(host)}\\storage</code> (Windows), '
                     f'<a href="smb://{e(host)}/storage">smb://{e(host)}/storage</a> '
                     f'(Mac, Linux, phones)</div>')
        elif cid == "ntfy" and c.get("server"):
            url = e(ntfy_link(c["server"], host))
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
            f'<div>{e(c["summary"])}{extra}{details_html(c["details"])}</div></div>')

    hostname = e(status["hostname"])
    # The box's clock is often UTC: the script shows it in the viewer's time zone, and without
    # it the text says which zone it is.
    updated = (f'<time datetime="{time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(status["time"]))}">'
               f'{time.strftime("%H:%M:%S %Z", time.localtime(status["time"]))}</time>')
    title = f"{hostname} · torrent-box" if worst == OK else f"⚠ {hostname} · torrent-box"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="{BRAND[0]}">
<title>{title}</title>
<link rel="icon" href="{favicon(worst)}">
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
<div class="grid">{"".join(tiles)}</div>
<h2>Health</h2>
<div class="rows">{"".join(rows)}</div>
<footer><span id="updated">Updated {updated}, refreshes every {REFRESH_SECONDS} s.</span>
<span>torrent-box · <a href="https://github.com/vibesclauder/torrent-box">GitHub</a></span></footer>
</main>
</div><script>{SCRIPT}</script></body></html>
"""


### Server

class Status:
    """The latest round of checks, shared by all requests for CACHE_SECONDS."""

    def __init__(self, config, arr_dir):
        self.config, self.arr_dir = config, arr_dir
        self.pool = ThreadPoolExecutor(max_workers=12)
        self.lock = threading.Lock()
        self.latest, self.at = None, 0.0
        self.login_lock = threading.Lock()
        self.tries = []  # times of recent 2FA logins
        self.last_login = None  # {"time", "ok", "message"}: shown on the page for a while
        self.logos = Logos()

    def get(self):
        with self.lock:
            if self.latest is None or time.monotonic() - self.at > CACHE_SECONDS:
                latest = gather(load_settings(self.config), self.arr_dir, self.pool,
                                self.last_login)
                latest["logos"] = self.logos.available(
                    [cid for cid, *_ in TILES if cid in latest["checks"]], self.pool)
                self.latest, self.at = latest, time.monotonic()
            return self.latest

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
                try:
                    ncore_login(self.arr_dir, code)
                    ok, message = True, ""
                except LoginError as e:
                    ok, message = False, str(e)
            self.last_login = {"time": time.time(), "ok": ok, "message": message}
            with self.lock:
                self.latest = None  # show it right away
            return ok, message
        finally:
            self.login_lock.release()


class Handler(BaseHTTPRequestHandler):
    server_version = "torrent-box-dashboard"
    status = None  # set in main()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            body = render(self.status.get(), link_host(self.headers.get("Host"),
                                                       socket.gethostname())).encode()
            kind = "text/html; charset=utf-8"
        elif path == "/status.json":
            body = json.dumps(self.status.get(), indent=1).encode()
            kind = "application/json"
        elif path.startswith("/icon/") and (logo := self.status.logos.get(path[6:])):
            body, kind = logo
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        if path.startswith("/icon/"):
            self.send_header("Cache-Control", "max-age=86400")
            # an SVG opened on its own is a document: no scripts from it on our page's origin
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
        else:
            self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
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

    Handler.status = Status(args.config, args.arr_dir)
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
