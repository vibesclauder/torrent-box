#!/usr/bin/env python3
"""rss2transmission - load torrents from the nCore bookmark RSS feed into Transmission.

One run, normally started every few minutes by a systemd timer:
  1. check that the storage disk is mounted
  2. fetch the feed; for every torrent not seen before: download it, pick the download
     folder from its nCore category, add it to Transmission, archive the .torrent
  3. do the same for .torrent files dropped by hand into the watch dir
  4. ask Transmission which torrents finished downloading since last time
  5. with Sonarr/Radarr/Whisparr: remove their downloads the library no longer has (replaced
     or deleted there), with the files, once nCore's seeding rule is met; with minimal_seed,
     remove every download from Transmission as soon as that rule is met
  6. check free space on the storage disk
  7. push a summary (added / downloaded / removed / problems) to the browsers the start page
     is installed in, and to ntfy if there's a topic
With a UPS ([setup] ups in config.ini), upsmon's notify hook also runs it with --ups-event: a
push about the power, a line in the grid log, and Transmission paused while on battery.
A torrent is only marked seen once Transmission accepted it, and a finished download only
counts as announced once ntfy accepted the push, so failures retry next run.
The very first run only records what is already in the feed and already finished in
Transmission, so nothing old is re-added or re-announced.

Exit status: 0 ok, 1 something failed this run (see the log), 2 bad config/usage.
"""

import argparse
import base64
import configparser
import contextlib
import fcntl
import json
import logging
import math
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

VERSION = ""  # "1.0 rev 2": setup.sh writes it in when installing
DEFAULT_CONFIG = Path("~/.config/rss2transmission/config.ini").expanduser()
DEFAULT_STATE = Path("~/.local/state/rss2transmission/state.sqlite").expanduser()
USER_AGENT = "rss2transmission/1.0"
NTFY_PRIORITIES = {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5}
WARN_EVERY = timedelta(hours=24)  # repeat mount/disk warnings at most this often
MAX_LIST_LINES = 20  # longer lists in a push are cut to "... and N more"
# [general] show_folders when it isn't set: nCore's series categories
DEFAULT_SHOW_FOLDERS = "hdser_hun, hdser, xvidser_hun, xvidser, dvdser_hun, dvdser"
# The routes that are Sonarr's root folders with [setup] arr = yes (setup.sh, setup_arr). Sonarr
# owns the show folders there: it imports any file it finds in one in place, and deletes it on
# an upgrade even while it's still seeding. So no show folders in them then.
SONARR_ROUTES = ("hdser_hun", "hdser")
# With [setup] arr = yes: where Sonarr, Radarr and Whisparr download to, under the storage root
# (setup.sh's arr-connect: <downloads>/series, /movies, /xxx). From there they hardlink the
# files into the library, so a torrent here holds no disk space of its own until the library
# lets go of its files.
ARR_DOWNLOADS = "downloads"
VIDEO_EXTENSIONS = frozenset((".mkv", ".mp4", ".m4v", ".avi", ".ts", ".m2ts", ".wmv", ".mov",
                              ".mpg", ".mpeg", ".webm"))
SAMPLE = re.compile(r"\bsample\b", re.I)
# With a UPS ([setup] ups): the name setup.sh gives it in NUT, the events upsmon hands over
# (templates/ups-notify), and two files beside the state database: the torrents stopped while
# on battery, and the grid log (when the mains went out and came back; kept for good).
UPS = "ups@127.0.0.1"
UPS_EVENTS = ("ONLINE", "ONBATT", "LOWBATT", "COMMOK", "COMMBAD", "NOCOMM", "SHUTDOWN", "REPLBATT")
UPS_PAUSED = "ups-paused.json"
GRID_LOG = "grid.log"
GRID_HEADER = ("# torrent-box: when the mains power went out and came back, as the UPS saw it.\n"
               "# Tab-separated: time, out / back (with how long it was out) / shutdown (the box\n"
               "# powered off on battery). Never rotated: it's a few lines a month.\n")

log = logging.getLogger("rss2transmission")


class ConfigError(Exception):
    pass


class TransmissionUnavailable(Exception):
    """Transmission can't be reached or refused our login: stop this run, retry later."""


class TorrentRejected(Exception):
    """Transmission answered, but refused this particular torrent."""


### Config

@dataclass
class Config:
    storage_root: Path
    require_mount: bool
    watch_dir: Path
    archive_dir: Path
    disk_warn_percent: int
    rpc_url: str
    timeout: float
    ntfy_server: str
    notify_added: bool
    notify_downloaded: bool
    priority_added: int
    priority_downloaded: int
    priority_problem: int
    routes: dict
    misc_dir: str
    show_categories: frozenset
    arr: bool
    remove_unused: bool
    minimal_seed: bool
    seed_ratio: float
    seed_hours: float
    seed_hours_per_gb: float
    notify_removed: bool
    priority_removed: int
    feed_url: str
    rpc_user: str
    rpc_password: str
    ntfy_topic: str
    pause_on_battery: bool = True
    dashboard_url: str = "http://127.0.0.1"  # the start page, which pushes to installed browsers
    push_token_file: str = "/var/lib/torrent-box-dashboard/push-token"


def _read_ini(path):
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str  # keep keys as written
    try:
        with open(path, encoding="utf-8") as f:
            parser.read_file(f)
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e.strerror}") from e
    except configparser.Error as e:
        raise ConfigError(f"{path}: {e}") from e
    return parser


def load_config(config_path, secrets_path=None):
    config_path = Path(config_path)
    secrets_path = Path(secrets_path) if secrets_path else config_path.parent / "secrets.ini"
    conf = _read_ini(config_path)
    secrets = _read_ini(secrets_path)

    def get(parser, path, section, key, fallback=None):
        try:
            return parser.get(section, key)
        except (configparser.NoSectionError, configparser.NoOptionError):
            if fallback is None:
                raise ConfigError(f"{path}: missing [{section}] {key}") from None
            return fallback

    def getbool(section, key, fallback):
        value = get(conf, config_path, section, key, str(fallback)).strip().lower()
        if value not in ("yes", "no", "true", "false", "on", "off", "1", "0"):
            raise ConfigError(f"{config_path}: [{section}] {key} must be yes or no")
        return value in ("yes", "true", "on", "1")

    def getnum(section, key, fallback, kind):
        value = get(conf, config_path, section, key, str(fallback))
        try:
            return kind(value)
        except ValueError:
            raise ConfigError(f"{config_path}: [{section}] {key} must be a number") from None

    def getpriority(key, fallback):
        value = get(conf, config_path, "ntfy", key, fallback).strip().lower()
        if value not in NTFY_PRIORITIES:
            raise ConfigError(f"{config_path}: [ntfy] {key} must be one of "
                              + ", ".join(NTFY_PRIORITIES))
        return NTFY_PRIORITIES[value]

    if not conf.has_section("routes"):
        raise ConfigError(f"{config_path}: missing [routes] section")
    routes = {code.strip(): folder.strip().strip("/") for code, folder in conf.items("routes")}
    misc_dir = routes.pop("misc_dir", "MISC")
    show_categories = frozenset(re.split(r"[\s,]+", get(
        conf, config_path, "general", "show_folders", DEFAULT_SHOW_FOLDERS).strip())) - {""}
    arr = get(conf, config_path, "setup", "arr", "").strip().lower() == "yes"
    if arr:
        show_categories -= set(SONARR_ROUTES)

    return Config(
        storage_root=Path(get(conf, config_path, "general", "storage_root")).expanduser(),
        require_mount=getbool("general", "require_mount", True),
        watch_dir=Path(get(conf, config_path, "general", "watch_dir")).expanduser(),
        archive_dir=Path(get(conf, config_path, "general", "archive_dir")).expanduser(),
        disk_warn_percent=getnum("general", "disk_warn_percent", 90, int),
        rpc_url=get(conf, config_path, "transmission", "rpc_url"),
        timeout=getnum("transmission", "timeout", 30, float),
        ntfy_server=get(conf, config_path, "ntfy", "server", "https://ntfy.sh").rstrip("/"),
        notify_added=getbool("ntfy", "notify_added", True),
        notify_downloaded=getbool("ntfy", "notify_downloaded", True),
        priority_added=getpriority("priority_added", "low"),
        priority_downloaded=getpriority("priority_downloaded", "default"),
        priority_problem=getpriority("priority_problem", "high"),
        routes=routes,
        misc_dir=misc_dir,
        show_categories=show_categories,
        arr=arr,
        remove_unused=getbool("cleanup", "remove_unused", True),
        minimal_seed=getbool("cleanup", "minimal_seed", True),
        seed_ratio=getnum("cleanup", "seed_ratio", 2.0, float),
        seed_hours=getnum("cleanup", "seed_hours", 50, float),
        seed_hours_per_gb=getnum("cleanup", "seed_hours_per_gb", 0.4, float),
        notify_removed=getbool("ntfy", "notify_removed", True),
        priority_removed=getpriority("priority_removed", "low"),
        feed_url=get(secrets, secrets_path, "secrets", "feed_url").strip(),
        rpc_user=get(secrets, secrets_path, "secrets", "rpc_user"),
        rpc_password=get(secrets, secrets_path, "secrets", "rpc_password"),
        ntfy_topic=get(secrets, secrets_path, "secrets", "ntfy_topic", "").strip(),
        pause_on_battery=getbool("transmission", "pause_on_battery", True),
        dashboard_url=get(conf, config_path, "push", "dashboard_url", "http://127.0.0.1").rstrip("/"),
        push_token_file=get(conf, config_path, "push", "token_file",
                            "/var/lib/torrent-box-dashboard/push-token"),
    )


### Helpers

def mask(text):
    """Hide the passkeys that appear in feed and download URLs."""
    text = re.sub(r"(key=)[^/&\s'\"]+", r"\1***", str(text))
    return re.sub(r"(/bookmarks/)[^/?\s'\"]+", r"\1***", text)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ncore_category(filename):
    """'[nCore][hd_hun]Some.Movie.torrent' -> 'hd_hun' (None if there is no such prefix)."""
    m = re.match(r"\[nCore\]\[([^\]]+)\]", filename)
    return m.group(1) if m else None


def route(cfg, filename):
    """Download folder for a torrent: its category's folder, and for series a folder per show
    inside it (SERIES/HD/HU/The Last of Us), so a media server sees one show, not one per
    season pack or episode."""
    category = ncore_category(filename)
    folder = cfg.storage_root / cfg.routes.get(category, cfg.misc_dir)
    if category in cfg.show_categories:
        show = show_name(display_name(filename))
        if show:
            return folder / show_folder(folder, show)
    return folder


# Where a series release name ends and the season/episode part begins.
SEASON_MARKER = re.compile(
    r"(?:^|[ ._-])(?:"
    r"S\d{1,4}(?:E\d{1,4}|(?=[ ._-]|$))"          # S01E02, S01, S01-S03
    r"|Season[ ._]?\d{1,2}(?=[ ._-]|$)"            # Season 1, Season.01
    r"|\d{1,2}\.?[ ._]?(?:evad|évad)(?=[ ._-]|$)"  # 1.evad, 2. évad
    r")", re.IGNORECASE)


def show_name(release):
    """'The.Last.of.Us.S02E03.1080p.WEB-DL' -> 'The Last of Us'. A year right before the
    season becomes '(year)': 'Doctor.Who.2005.S01' -> 'Doctor Who (2005)'. Initials stay
    together: 'S.W.A.T.2017.S07E01' -> 'S.W.A.T (2017)'. None if there's no season marker or
    no name before it (then it isn't treated as a series)."""
    m = SEASON_MARKER.search(release)
    if not m:
        return None
    words, initials = [], []
    for word in re.split(r"[ ._]+", release[:m.start()]) + [""]:
        if len(word) == 1 and word.isalpha():
            initials.append(word)
            continue
        words += [".".join(initials)] if len(initials) > 1 else initials
        initials = []
        if word:
            words.append(word)
    name = " ".join(words).strip(" -")
    year = re.fullmatch(r"(.+) \(?((?:19|20)\d\d)\)?", name)
    if year:
        name = f"{year.group(1)} ({year.group(2)})"
    name = name.replace("/", " ").strip(" .")[:100].strip()
    return name or None


def _show_key(name):
    """What makes two folder names the same show: (name without case and punctuation, year
    or None). 'Doctor Who' matches 'Doctor Who (2005)', but that doesn't match '(1963)'."""
    m = re.search(r"\s*\(((?:19|20)\d\d)\)$", name)
    base, year = (name[:m.start()], m.group(1)) if m else (name, None)
    return re.sub(r"[\W_]+", "", base.casefold()), year


def show_folder(parent, name):
    """Reuse the folder already there for this show (also one renamed by hand to e.g.
    'The Last Of Us (2023)'), else the new name. Transmission creates it when adding."""
    try:
        existing = sorted(e.name for e in os.scandir(parent) if e.is_dir())
    except OSError:
        return name
    if name in existing:
        return name
    base, year = _show_key(name)
    matches = []
    for e in existing:
        e_base, e_year = _show_key(e)
        if e_base == base and (year is None or e_year is None or e_year == year):
            matches.append((e_year != year, e))  # the same year (or none) first
    return min(matches)[1] if matches else name


def safe_filename(name):
    name = os.path.basename(name.replace("\\", "/")).strip().lstrip(".")
    name = name or "unnamed"
    return name if name.endswith(".torrent") else name + ".torrent"


def display_name(filename):
    """'[nCore][hd_hun]Some.Movie.torrent' -> 'Some.Movie'"""
    name = re.sub(r"^\[nCore\]\[[^\]]+\]", "", filename)
    return name[:-len(".torrent")] if name.endswith(".torrent") else name


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def short_dir(cfg, path):
    """Folder relative to the storage root when it's under it, else as is."""
    try:
        return str(Path(path).relative_to(cfg.storage_root))
    except ValueError:
        return str(path)


def bullet_list(lines):
    shown = [f"• {line}" for line in lines[:MAX_LIST_LINES]]
    if len(lines) > MAX_LIST_LINES:
        shown.append(f"… and {len(lines) - MAX_LIST_LINES} more")
    return "\n".join(shown)


def http_get(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(), resp.headers


### State

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    ncore_id     TEXT PRIMARY KEY,
    title        TEXT,
    info_hash    TEXT,
    category     TEXT,
    download_dir TEXT,
    added_at     TEXT
);
CREATE TABLE IF NOT EXISTS announced (
    info_hash    TEXT PRIMARY KEY,
    name         TEXT,
    announced_at TEXT
);
CREATE TABLE IF NOT EXISTS imported (
    info_hash    TEXT PRIMARY KEY,
    name         TEXT,
    imported_at  TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class State:
    """SQLite store of what has been handled. readonly=True (dry runs) never writes the file."""

    def __init__(self, path, readonly=False):
        path = Path(path)
        if readonly:
            if path.exists():
                self.db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
                # a file from an older version lacks the newer tables: empty ones for this run
                present = {row[0] for row in self.db.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'")}
                for statement in SCHEMA.split(";"):
                    table = re.search(r"CREATE TABLE IF NOT EXISTS (\w+)", statement)
                    if table and table.group(1) not in present:
                        self.db.execute(statement.replace("CREATE TABLE", "CREATE TEMP TABLE"))
            else:
                self.db = sqlite3.connect(":memory:")
                self.db.executescript(SCHEMA)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(path, isolation_level=None)  # autocommit
            self.db.executescript(SCHEMA)

    def get_meta(self, key):
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    @property
    def initialized(self):
        return self.get_meta("initialized") is not None

    def mark_initialized(self):
        self.set_meta("initialized", now())

    def is_seen(self, ncore_id):
        return self.db.execute("SELECT 1 FROM seen WHERE ncore_id = ?", (ncore_id,)).fetchone() is not None

    def mark_seen(self, ncore_id, title, info_hash=None, category=None, download_dir=None):
        self.db.execute(
            "INSERT OR REPLACE INTO seen VALUES (?, ?, ?, ?, ?, ?)",
            (ncore_id, title, info_hash, category, download_dir, now()))

    def is_announced(self, info_hash):
        return self.db.execute("SELECT 1 FROM announced WHERE info_hash = ?",
                               (info_hash,)).fetchone() is not None

    def mark_announced(self, info_hash, name):
        self.db.execute("INSERT OR REPLACE INTO announced VALUES (?, ?, ?)",
                        (info_hash, name, now()))

    def is_imported(self, info_hash):
        return self.db.execute("SELECT 1 FROM imported WHERE info_hash = ?",
                               (info_hash,)).fetchone() is not None

    def mark_imported(self, info_hash, name):
        self.db.execute("INSERT OR REPLACE INTO imported VALUES (?, ?, ?)",
                        (info_hash, name, now()))

    def forget_imported(self, info_hash):
        self.db.execute("DELETE FROM imported WHERE info_hash = ?", (info_hash,))

    def warning_due(self, key):
        last = self.get_meta(key)
        return last is None or datetime.now(timezone.utc) - datetime.fromisoformat(last) >= WARN_EVERY

    def clear_warning(self, key):
        self.db.execute("DELETE FROM meta WHERE key = ?", (key,))

    @contextlib.contextmanager
    def transaction(self):
        """Group many writes into one commit (one disk flush instead of one per row)."""
        self.db.execute("BEGIN")
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self.db.execute("COMMIT")

    def close(self):
        self.db.close()


### Transmission RPC

class Transmission:
    def __init__(self, url, user, password, timeout=30):
        self.url = url
        self.timeout = timeout
        self.session_id = ""
        self.add_paused = False  # on battery (UPS): new torrents wait for the power too
        self.auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()

    def call(self, method, arguments=None):
        body = json.dumps({"method": method, "arguments": arguments or {}}).encode()
        for _ in range(2):  # a 409 hands us a fresh session id; retry once with it
            req = urllib.request.Request(self.url, data=body, headers={
                "Authorization": self.auth,
                "X-Transmission-Session-Id": self.session_id,
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            })
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    reply = json.loads(resp.read())
            except urllib.error.HTTPError as e:
                if e.code == 409:
                    self.session_id = e.headers.get("X-Transmission-Session-Id", "")
                    continue
                if e.code == 401:
                    raise TransmissionUnavailable("login refused (check rpc_user/rpc_password)") from e
                raise TransmissionUnavailable(f"HTTP {e.code} {e.reason}") from e
            except (urllib.error.URLError, OSError, ValueError) as e:
                raise TransmissionUnavailable(str(getattr(e, "reason", e))) from e
            if reply.get("result") != "success":
                raise TorrentRejected(reply.get("result", "no result in reply"))
            return reply.get("arguments", {})
        raise TransmissionUnavailable("could not obtain an RPC session id")

    def add(self, metainfo, download_dir):
        """Add a torrent. Returns ("added" | "duplicate", info_hash)."""
        args = self.call("torrent-add", {
            "metainfo": base64.b64encode(metainfo).decode(),
            "download-dir": str(download_dir),
            **({"paused": True} if self.add_paused else {}),
        })
        if "torrent-duplicate" in args:
            return "duplicate", args["torrent-duplicate"].get("hashString")
        return "added", args.get("torrent-added", {}).get("hashString")


    def finished(self, extra_fields=()):
        """Torrents that are fully downloaded: list of dicts with hashString, name,
        downloadDir, sizeWhenDone and the extra_fields asked for."""
        args = self.call("torrent-get", {"fields": [
            "hashString", "name", "percentDone", "downloadDir", "sizeWhenDone",
            *extra_fields]})
        return [t for t in args.get("torrents", []) if t.get("percentDone", 0) >= 1]

    def remove(self, info_hash, delete_files=True):
        """Remove a torrent, and delete its files unless told not to."""
        self.call("torrent-remove", {"ids": [info_hash], "delete-local-data": delete_files})


### ntfy

class NotifyError(Exception):
    pass


class Ntfy:
    """Push notifications: to the browsers the start page is installed in (best effort, when
    this box has the start page's token) and via ntfy (https://ntfy.sh or self-hosted), when
    there's a topic. Disabled if there's neither. Without a topic the start page is the only
    way, and a push it didn't take is an error (so it's tried again)."""

    def __init__(self, server, topic, timeout=30, dashboard=None):
        self.server = server
        self.topic = topic
        self.timeout = timeout
        self.dashboard = dashboard  # (the start page's address, the file with its token)

    def token(self):
        if not self.dashboard:
            return ""
        try:
            return Path(self.dashboard[1]).read_text().strip()
        except OSError:
            return ""

    @property
    def enabled(self):
        return bool(self.topic) or bool(self.token())

    def send_dashboard(self, title, message):
        """Hand the push to the start page; the reason why not, or "" when it took it."""
        body = json.dumps({"title": title, "message": message}).encode()
        req = urllib.request.Request(self.dashboard[0] + "/push/notify", data=body, headers={
            "Content-Type": "application/json", "User-Agent": USER_AGENT,
            "Authorization": f"Bearer {self.token()}"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp.read()
        except urllib.error.HTTPError as e:
            return f"the start page answered HTTP {e.code} {e.reason}"
        except (urllib.error.URLError, OSError) as e:
            return f"the start page is unreachable: {getattr(e, 'reason', e)}"
        return ""

    def send(self, title, message, priority=3, tags=()):
        failed = self.send_dashboard(title, message) if self.token() else "no start page token"
        if not self.topic:
            if failed:
                raise NotifyError(failed)
            return
        # JSON publishing (POST to the server root) rather than headers, so non-ASCII
        # titles like Hungarian release names survive.
        body = json.dumps({"topic": self.topic, "title": title, "message": message,
                           "priority": priority, "tags": list(tags)}).encode()
        req = urllib.request.Request(self.server + "/", data=body, headers={
            "Content-Type": "application/json", "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp.read()
        except urllib.error.HTTPError as e:
            raise NotifyError(f"ntfy answered HTTP {e.code} {e.reason}") from e
        except (urllib.error.URLError, OSError) as e:
            raise NotifyError(f"ntfy unreachable: {getattr(e, 'reason', e)}") from e


### Feed

@dataclass
class FeedItem:
    ncore_id: str
    title: str
    link: str


def parse_feed(data):
    """Feed XML -> items, oldest first (the feed lists newest first)."""
    items = []
    for item in ET.fromstring(data).iter("item"):
        link = (item.findtext("link") or "").strip()
        if not link:
            continue
        m = re.search(r"id=(\d+)", link)
        ncore_id = m.group(1) if m else re.sub(r"/key=[^/&]+", "", link)
        items.append(FeedItem(ncore_id, (item.findtext("title") or "").strip(), link))
    items.reverse()
    return items


def download_torrent(cfg, item):
    data, headers = http_get(item.link, cfg.timeout)
    if not data.startswith(b"d"):
        raise ValueError("response is not a .torrent file")
    m = re.search(r'filename="?([^";]+)"?', headers.get("Content-Disposition", ""))
    return safe_filename(m.group(1) if m else item.title), data


### Run

@dataclass
class Added:
    filename: str
    download_dir: Path
    status: str  # "added", "duplicate" or "dry-run"
    source: str  # "feed" or "watch"


@dataclass
class Report:
    added: list = field(default_factory=list)
    completed: list = field(default_factory=list)
    removed: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    seeded: int = 0
    seeded_completions: int = 0

    def error(self, message):
        log.error("%s", message)
        self.errors.append(message)


def push(ntfy, report, dry_run, title, message, priority, tags):
    """Send a push; returns False (and records an error) if ntfy didn't take it."""
    if not ntfy.enabled:
        return True
    if dry_run:
        log.info("[dry-run] would push: %s\n%s", title, message)
        return True
    try:
        ntfy.send(title, message, priority, tags)
        return True
    except NotifyError as e:
        report.error(f"push '{title}' failed: {e}")
        return False


def warn(cfg, state, ntfy, report, dry_run, key, title, message):
    """Problem push, repeated at most every WARN_EVERY while the problem lasts."""
    if not state.warning_due(key):
        return
    if push(ntfy, report, dry_run, title, message, cfg.priority_problem, ["warning"]) \
            and not dry_run:
        state.set_meta(key, now())


def clear_warning(state, dry_run, key):
    """Problem is gone: the next occurrence should warn right away."""
    if not dry_run and state.get_meta(key) is not None:
        state.clear_warning(key)


def load(cfg, tr, filename, metainfo, source, dry_run):
    """Add one torrent to Transmission and archive its file. Raises TransmissionUnavailable /
    TorrentRejected."""
    download_dir = route(cfg, filename)
    if dry_run:
        log.info("[dry-run] would add %s to %s", filename, download_dir)
        return Added(filename, download_dir, "dry-run", source), None
    status, info_hash = tr.add(metainfo, download_dir)
    cfg.archive_dir.mkdir(parents=True, exist_ok=True)
    (cfg.archive_dir / filename).write_bytes(metainfo)
    if status == "duplicate":
        log.info("already in transmission: %s", filename)
    else:
        log.info("added %s to %s", filename, download_dir)
    return Added(filename, download_dir, status, source), info_hash


def check_mount(cfg, state, ntfy, report, dry_run):
    if not cfg.require_mount or os.path.ismount(cfg.storage_root):
        clear_warning(state, dry_run, "mount_warned_at")
        return True
    message = f"{cfg.storage_root} is not mounted; not adding anything this run"
    report.error(message)
    warn(cfg, state, ntfy, report, dry_run, "mount_warned_at", "Storage disk not mounted",
         message + ". New torrents wait until it's back.")
    return False


def process_feed(cfg, state, tr, report, dry_run, seed):
    """Returns False if Transmission was unavailable (no point going on this run)."""
    try:
        data, _ = http_get(cfg.feed_url, cfg.timeout)
        items = parse_feed(data)
    except Exception as e:
        report.error(f"fetching the feed failed: {mask(e)}")
        return True

    if seed:
        if not dry_run:
            with state.transaction():
                for item in items:
                    if not state.is_seen(item.ncore_id):
                        state.mark_seen(item.ncore_id, item.title)
                state.mark_initialized()
        report.seeded = len(items)
        log.info("%smarked %d current feed items as seen without adding them",
                 "[dry-run] would have " if dry_run else "", len(items))
        return True

    for item in items:
        if state.is_seen(item.ncore_id):
            continue
        try:
            filename, metainfo = download_torrent(cfg, item)
        except Exception as e:
            report.error(f"downloading '{item.title}' failed, will retry: {mask(e)}")
            continue
        try:
            added, info_hash = load(cfg, tr, filename, metainfo, "feed", dry_run)
        except TransmissionUnavailable as e:
            report.error(f"transmission unavailable, will retry next run: {e}")
            return False
        except TorrentRejected as e:
            report.error(f"transmission refused {filename}, will retry: {e}")
            continue
        if not dry_run:
            state.mark_seen(item.ncore_id, item.title, info_hash,
                            ncore_category(filename), str(added.download_dir))
        report.added.append(added)
    return True


def process_watch_dir(cfg, tr, report, dry_run):
    """Returns False if Transmission was unavailable."""
    if not cfg.watch_dir.is_dir():
        return True
    for path in sorted(cfg.watch_dir.glob("*.torrent"), key=lambda p: p.stat().st_mtime):
        try:
            metainfo = path.read_bytes()
            added, _ = load(cfg, tr, path.name, metainfo, "watch", dry_run)
        except TransmissionUnavailable as e:
            report.error(f"transmission unavailable, will retry next run: {e}")
            return False
        except (TorrentRejected, OSError) as e:
            report.error(f"could not add {path.name}: {e}")
            continue
        if not dry_run:
            path.unlink()
        report.added.append(added)
    return True


def notify_added(cfg, ntfy, report, dry_run):
    if not report.added or not cfg.notify_added:
        return
    def where(a):
        dup = " (was already in Transmission)" if a.status == "duplicate" else ""
        return f"→ {short_dir(cfg, a.download_dir)}{dup}"

    if len(report.added) == 1:  # the name is in the title; the body just says where
        a = report.added[0]
        title, message = f"Added: {display_name(a.filename)}", where(a)
    else:
        title = f"{len(report.added)} torrents added"
        message = bullet_list([f"{display_name(a.filename)} {where(a)}" for a in report.added])
    push(ntfy, report, dry_run, title, message, cfg.priority_added, ["arrow_down"])


def process_completions(cfg, state, tr, ntfy, report, dry_run, seed):
    try:
        finished = tr.finished()
    except (TransmissionUnavailable, TorrentRejected) as e:
        report.error(f"could not ask transmission for finished downloads: {e}")
        return
    new = [t for t in finished if not state.is_announced(t["hashString"])]

    if seed:
        if not dry_run:
            with state.transaction():
                for t in new:
                    state.mark_announced(t["hashString"], t.get("name"))
                state.set_meta("completions_initialized", now())
        report.seeded_completions = len(new)
        log.info("%smarked %d already finished downloads as announced",
                 "[dry-run] would have " if dry_run else "", len(new))
        return
    if not new:
        return

    for t in new:
        log.info("finished: %s", t.get("name"))
    if cfg.notify_downloaded:
        def details(t):
            return (f"{human_size(t.get('sizeWhenDone', 0))} → "
                    f"{short_dir(cfg, t.get('downloadDir', ''))}")

        if len(new) == 1:  # the name is in the title; the body just says size and where
            title, message = f"Downloaded: {new[0].get('name')}", details(new[0])
        else:
            title = f"{len(new)} downloads finished"
            message = bullet_list([f"{t.get('name')} ({details(t)})" for t in new])
        if not push(ntfy, report, dry_run, title, message,
                    cfg.priority_downloaded, ["white_check_mark"]):
            return  # not marked announced -> retried next run
    report.completed = new
    if not dry_run:
        with state.transaction():
            for t in new:
                state.mark_announced(t["hashString"], t.get("name"))


def file_links(torrent):
    """Hardlink count of each of a torrent's files, or None if one can't be read (gone, or
    the disk is away): then nothing is decided about the torrent this run."""
    links = {}
    for f in torrent.get("files", []):
        try:
            links[f["name"]] = os.stat(Path(torrent["downloadDir"]) / f["name"]).st_nlink
        except OSError:
            return None
    return links


def imported(links):
    """The app has hardlinked every video file (samples aside) into the library."""
    videos = [n for name, n in links.items()
              if Path(name).suffix.lower() in VIDEO_EXTENSIONS and not SAMPLE.search(name)]
    return bool(videos) and all(n > 1 for n in videos)


def seeding_done(cfg, t):
    """nCore's rule as configured: the ratio, or the hours (more for bigger torrents)."""
    hours = cfg.seed_hours + cfg.seed_hours_per_gb * t.get("sizeWhenDone", 0) / 1e9
    return (t.get("uploadRatio", 0) >= cfg.seed_ratio
            or t.get("secondsSeeding", 0) >= hours * 3600)


def process_removals(cfg, state, tr, ntfy, report, dry_run):
    """Sonarr/Radarr/Whisparr downloads: remember which ones were imported, and remove those
    the library has let go of since (every file back to one link: replaced by another
    release, or deleted) once seeding is done. A download that was never imported (failed,
    or waiting for a manual import) is never removed.
    With minimal_seed, every download leaves Transmission as soon as seeding is done: an
    imported arr download with its files (the library keeps its own hardlinks), anything
    outside the arr downloads folder without them (those files are the library). An arr
    download that was never imported still stays."""
    root = cfg.storage_root / ARR_DOWNLOADS
    try:
        finished = tr.finished(["files", "uploadRatio", "secondsSeeding"])
    except (TransmissionUnavailable, TorrentRejected) as e:
        report.error(f"could not ask transmission for seeding downloads: {e}")
        return
    due = []  # (torrent, delete its files)
    for t in finished:
        if not (cfg.arr and Path(t.get("downloadDir", "")).is_relative_to(root)):
            if cfg.minimal_seed and seeding_done(cfg, t):
                t["freed"] = 0
                due.append((t, False))
            continue
        links = file_links(t)
        if not links:
            continue
        if imported(links):
            if not dry_run and not state.is_imported(t["hashString"]):
                state.mark_imported(t["hashString"], t.get("name"))
            if cfg.minimal_seed and seeding_done(cfg, t):
                t["freed"] = 0  # the library still holds the files
                due.append((t, True))
        elif (all(n == 1 for n in links.values()) and state.is_imported(t["hashString"])
              and seeding_done(cfg, t)):
            t["freed"] = t.get("sizeWhenDone", 0)
            due.append((t, True))

    for t, delete_files in due:
        if dry_run:
            log.info("[dry-run] would remove %s%s", t.get("name"),
                     "" if delete_files else " (keeping its files)")
            continue
        try:
            tr.remove(t["hashString"], delete_files)
        except TorrentRejected as e:
            report.error(f"could not remove {t.get('name')}: {e}")
            continue
        except TransmissionUnavailable as e:
            report.error(f"could not remove {t.get('name')}: {e}")
            break
        state.forget_imported(t["hashString"])
        log.info("removed %s (ratio %.2f, seeded %.0f h)", t.get("name"),
                 t.get("uploadRatio", 0), t.get("secondsSeeding", 0) / 3600)
        report.removed.append(t)
    if not report.removed or not cfg.notify_removed:
        return

    def details(t):
        return (f"{human_size(t['freed']) + ' freed' if t['freed'] else 'files kept'}, ratio "
                f"{max(t.get('uploadRatio', 0), 0):.2f}, seeded "
                f"{t.get('secondsSeeding', 0) / 3600:.0f} h")

    if len(report.removed) == 1:
        title, message = f"Removed: {report.removed[0].get('name')}", details(report.removed[0])
    else:
        freed = sum(t["freed"] for t in report.removed)
        title = (f"{len(report.removed)} seeded downloads removed"
                 + (f", {human_size(freed)} freed" if freed else ""))
        message = bullet_list([f"{t.get('name')} ({details(t)})" for t in report.removed])
    push(ntfy, report, dry_run, title, message, cfg.priority_removed, ["wastebasket"])


def check_disk(cfg, state, ntfy, report, dry_run):
    try:
        usage = shutil.disk_usage(cfg.storage_root)
    except OSError as e:
        report.error(f"cannot check free space on {cfg.storage_root}: {e}")
        return
    # same as df's Use%: blocks reserved for root count as neither used nor free, rounded up
    percent = math.ceil(100 * usage.used / (usage.used + usage.free)) if usage.used else 0
    if percent < cfg.disk_warn_percent:
        clear_warning(state, dry_run, "disk_warned_at")
        return
    log.warning("%s is %d%% full", cfg.storage_root, percent)
    warn(cfg, state, ntfy, report, dry_run, "disk_warned_at",
         f"Storage disk {percent}% full",
         f"{cfg.storage_root}: {human_size(usage.free)} free of {human_size(usage.total)}")


def run(cfg, state, tr, ntfy, dry_run=False, catch_up=False):
    report = Report()
    mounted = check_mount(cfg, state, ntfy, report, dry_run)
    transmission_ok = True
    if mounted:
        transmission_ok = (process_feed(cfg, state, tr, report, dry_run,
                                        seed=catch_up or not state.initialized)
                           and process_watch_dir(cfg, tr, report, dry_run))
        notify_added(cfg, ntfy, report, dry_run)
    if transmission_ok:
        seed = catch_up or state.get_meta("completions_initialized") is None
        process_completions(cfg, state, tr, ntfy, report, dry_run, seed)
        if mounted and ((cfg.arr and cfg.remove_unused) or cfg.minimal_seed):
            process_removals(cfg, state, tr, ntfy, report, dry_run)
    if mounted:
        check_disk(cfg, state, ntfy, report, dry_run)
    return report


### UPS (upsmon's notify hook, templates/ups-notify)

def upsc(ups=UPS, timeout=5):
    """The UPS's variables from NUT ({} if it can't tell)."""
    try:
        out = subprocess.run(["upsc", ups], capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    return dict(line.split(": ", 1) for line in out.splitlines() if ": " in line)


def ups_summary(v):
    """ "battery 87%, about 23 min left, load 14% (32 W)" from upsc's variables."""
    parts = []
    if v.get("battery.charge"):
        parts.append(f"battery {v['battery.charge']}%")
    try:
        parts.append(f"about {duration(float(v['battery.runtime']))} left")
    except (KeyError, ValueError):
        pass
    watts = ups_watts(v)
    if v.get("ups.load"):
        parts.append(f"load {v['ups.load']}%" + (f" ({watts})" if watts else ""))
    elif watts:
        parts.append(f"drawing {watts}")
    return ", ".join(parts)


def ups_watts(v):
    """What the load draws: "32 W"; "139 VA" when the UPS's watts can't be trusted (some
    Eatons say 0 W at times while the load is well above 0, and VA is all that's left: a bit
    more than the watts); "about 30 W" from its percent of what it's rated for; "" if the
    UPS doesn't tell."""
    def num(key):
        try:
            return float(v[key])
        except (KeyError, ValueError):
            return None
    watts, load, va = num("ups.realpower"), num("ups.load"), num("ups.power")
    if watts is not None and (watts > 0 or not load):
        return f"{watts:.0f} W"
    if va:
        return f"{va:.0f} VA"
    if load is not None and num("ups.realpower.nominal"):
        return f"about {load * num('ups.realpower.nominal') / 100:.0f} W"
    return ""


def duration(seconds):
    seconds = max(0, round(seconds))
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        m, s = divmod(seconds, 60)
        return f"{m} min {s} s" if s and m < 10 else f"{m} min"
    h, m = divmod(round(seconds / 60), 60)
    return f"{h} h {m} min" if m else f"{h} h"


def grid_events(path):
    """The grid log as [(datetime, event, rest...)], oldest first."""
    events = []
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return events
    for line in lines:
        fields = line.split("\t")
        if line.startswith("#") or len(fields) < 2:
            continue
        try:
            events.append((datetime.fromisoformat(fields[0]), *fields[1:]))
        except ValueError:
            continue
    return events


def grid_append(path, when, event, *rest):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        if f.tell() == 0:
            f.write(GRID_HEADER)
        f.write("\t".join([when.astimezone().isoformat(timespec="seconds"), event, *rest]) + "\n")


def grid_out_since(path):
    """When the mains went out, if the log's last word is that it's out (or that the box
    powered off on battery); else None."""
    events = grid_events(path)
    if events and events[-1][1] in ("out", "shutdown"):
        for e in reversed(events):
            if e[1] == "out":
                return e[0]
        return events[-1][0]
    return None


def grid_back(path, when, note=""):
    """Log the mains coming back; how long it was out, or None if it wasn't out."""
    since = grid_out_since(path)
    if since is None:
        return None
    seconds = (when - since).total_seconds()
    grid_append(path, when, "back", duration(seconds), *([note] if note else []))
    return seconds


def boot_time():
    with open("/proc/uptime") as f:
        return datetime.now(timezone.utc) - timedelta(seconds=float(f.read().split()[0]))


def grid_after_boot(path, ups_vars=upsc, booted=boot_time):
    """The box powered off on battery and has come back: upsmon doesn't say ONLINE when it
    starts, so the mains came back when the box booted (the UPS turned its outlets on).
    Run with every timer run; only looks at the UPS while the log says the power is out."""
    since = grid_out_since(path)
    if since is None:
        return None
    try:
        boot = booted()
    except (OSError, ValueError):
        return None
    if boot <= since or "OL" not in ups_vars().get("ups.status", "").split():
        return None
    log.info("the mains is back (the box booted at %s)", boot.astimezone().isoformat(timespec="seconds"))
    return grid_back(path, boot, "the box was off: this is when it booted")


def pause_torrents(tr, paused_file):
    """Stop every torrent that's running; their hashes go to paused_file, for resuming."""
    torrents = tr.call("torrent-get", {"fields": ["hashString", "status"]}).get("torrents", [])
    running = [t["hashString"] for t in torrents if t.get("status")]  # 0: stopped
    earlier = []
    with contextlib.suppress(OSError, ValueError):
        earlier = json.loads(Path(paused_file).read_text())  # still out since an earlier event
    if running:
        tr.call("torrent-stop", {"ids": running})
    Path(paused_file).parent.mkdir(parents=True, exist_ok=True)
    Path(paused_file).write_text(json.dumps(sorted(set(earlier) | set(running))))
    return len(running)


def resume_torrents(tr, paused_file):
    """Start the torrents pause_torrents stopped (not those you had stopped yourself)."""
    try:
        hashes = json.loads(Path(paused_file).read_text())
    except (OSError, ValueError):
        return 0
    if hashes:
        tr.call("torrent-start", {"ids": hashes})
    Path(paused_file).unlink()
    return len(hashes)


# event: (title, priority, tags); {host} is the box's name
UPS_PUSHES = {
    "ONBATT": ("Power cut: {host} is on battery", "high", ["zap", "warning"]),
    "LOWBATT": ("UPS battery low: {host} shuts down now", "urgent", ["battery", "rotating_light"]),
    "SHUTDOWN": ("{host} is shutting down (UPS battery low)", "urgent", ["battery", "rotating_light"]),
    "ONLINE": ("Power is back on {host}", "default", ["electric_plug"]),
    "COMMBAD": ("{host} lost touch with the UPS", "high", ["warning"]),
    "NOCOMM": ("{host} can't reach the UPS", "high", ["warning"]),
    "COMMOK": ("{host} reaches the UPS again", "default", ["white_check_mark"]),
    "REPLBATT": ("The UPS battery of {host} needs replacing", "default", ["battery"]),
}


def ups_event(cfg, tr, ntfy, event, state_dir, ups_vars=upsc, when=None):
    """Handle one of upsmon's events: the grid log, pausing / resuming Transmission, a push.
    Returns the exit status. None of this is needed for the shutdown: upsmon does that."""
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    # upsmon runs its hook for each event without waiting: on a flicker, ONLINE mustn't
    # resume before ONBATT has paused
    with open(state_dir / "ups.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _ups_event(cfg, tr, ntfy, event, state_dir, ups_vars,
                          when or datetime.now(timezone.utc))


def _ups_event(cfg, tr, ntfy, event, state_dir, ups_vars, when):
    grid, paused_file = state_dir / GRID_LOG, state_dir / UPS_PAUSED
    v = ups_vars()
    lines = [s] if (s := ups_summary(v)) else []
    title, priority, tags = UPS_PUSHES[event]
    status = v.get("ups.status", "").split()
    if event == "LOWBATT" and "OL" in status and "OB" not in status:
        # NUT works "low" out from the runtime left at the present load (ignorelb), on mains
        # too: a heavy load on the UPS (a PC sharing it) gets the estimate under the limit at
        # full charge. upsmon shuts down only on battery, so this is no news.
        log.info("UPS low battery while on mains (%s): no shutdown, no push", ups_summary(v))
        return 0
    if event == "ONBATT":
        if grid_out_since(grid) is None:
            grid_append(grid, when, "out")
        if cfg.pause_on_battery:
            try:
                n = pause_torrents(tr, paused_file)
                lines.append(f"Transmission: {n} torrent{'s' if n != 1 else ''} paused until the "
                             "power is back" if n else "Transmission: nothing was running")
            except (TransmissionUnavailable, TorrentRejected) as e:
                lines.append(f"Transmission couldn't be paused: {e}")
    elif event == "ONLINE":
        seconds = grid_back(grid, when)
        resumed = None
        try:
            resumed = resume_torrents(tr, paused_file)
        except (TransmissionUnavailable, TorrentRejected) as e:
            lines.append(f"Transmission couldn't be resumed: {e} (the torrents are still paused)")
        if seconds is None and not resumed:
            log.info("UPS on mains (it wasn't out as far as the grid log knows): no push")
            return 0
        if seconds is not None:
            title += f" after {duration(seconds)}"
        if resumed:
            lines.append(f"Transmission: {resumed} torrent{'s' if resumed != 1 else ''} resumed")
    elif event == "SHUTDOWN":
        if grid_out_since(grid) is not None:
            grid_append(grid, when, "shutdown")
        lines.append("It starts again by itself when the power is back (if its BIOS powers on "
                     "after a power loss).")
    elif event == "REPLBATT":
        lines.append("The UPS's self-test says so: the runtime it shows can't be trusted any more.")
    elif event in ("COMMBAD", "NOCOMM"):
        lines.append("Without it the box can't shut down cleanly on a power cut. Check the USB "
                     "cable; details: journalctl -u nut-driver@ups -u nut-monitor")
    message = "\n".join(lines) or "upsc ups for the details"
    title = title.format(host=socket.gethostname())
    if not ntfy.enabled:
        log.info("%s (notifications are off)", title)
        return 0
    try:
        ntfy.send(title, message, NTFY_PRIORITIES[priority], tags)
    except NotifyError as e:
        log.error("push '%s' failed: %s", title, e)
        return 1
    log.info("pushed: %s", title)
    return 0


### Main Entry

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("-c", "--config", type=Path, default=DEFAULT_CONFIG,
                    help=f"config file (default {DEFAULT_CONFIG}); secrets.ini is read from "
                         "the same directory")
    ap.add_argument("--state", type=Path, default=DEFAULT_STATE,
                    help=f"state database (default {DEFAULT_STATE})")
    ap.add_argument("--dry-run", action="store_true",
                    help="only log what would happen; no Transmission changes, no state "
                         "changes, no pushes")
    ap.add_argument("--catch-up", action="store_true",
                    help="mark everything currently in the feed as seen and everything already "
                         "finished in Transmission as announced, without adding/pushing")
    ap.add_argument("--test-notify", action="store_true",
                    help="send a test push to the configured ntfy topic and exit")
    ap.add_argument("--ups-event", choices=UPS_EVENTS,
                    help="upsmon's event (its notify hook runs this): push it, log the grid, "
                         "pause or resume Transmission, and exit")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr,
        format="%(levelname)s %(message)s" if os.environ.get("JOURNAL_STREAM")
        else "%(asctime)s %(levelname)s %(message)s")

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        log.error("%s", e)
        return 2
    ntfy = Ntfy(cfg.ntfy_server, cfg.ntfy_topic, cfg.timeout,
                dashboard=(cfg.dashboard_url, cfg.push_token_file))

    if args.test_notify:
        if not ntfy.enabled:
            log.error("no ntfy_topic in secrets.ini and no start page to push through; "
                      "notifications are off")
            return 2
        try:
            version = f"\ntorrent-box {VERSION}" if VERSION else ""
            ntfy.send("torrent-box test",
                      f"Notifications from {socket.gethostname()} work.{version}",
                      cfg.priority_downloaded, ["tada"])
        except NotifyError as e:
            log.error("%s", e)
            return 1
        log.info("test push sent%s", f" via {cfg.ntfy_server}" if cfg.ntfy_topic else "")
        return 0
    if args.ups_event:
        tr = Transmission(cfg.rpc_url, cfg.rpc_user, cfg.rpc_password, min(cfg.timeout, 10))
        return ups_event(cfg, tr, ntfy, args.ups_event, args.state.parent)
    if not ntfy.enabled:
        log.info("no ntfy_topic in secrets.ini and no start page to push through; notifications are off")

    args.state.parent.mkdir(parents=True, exist_ok=True)
    with open(args.state.with_suffix(".lock"), "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.info("another run is in progress; skipping")
            return 0
        state = State(args.state, readonly=args.dry_run)
        try:
            tr = Transmission(cfg.rpc_url, cfg.rpc_user, cfg.rpc_password, cfg.timeout)
            tr.add_paused = (args.state.parent / UPS_PAUSED).exists()  # on battery
            if not args.dry_run:
                try:
                    grid_after_boot(args.state.parent / GRID_LOG)
                except OSError as e:
                    log.warning("grid log: %s", e)
            report = run(cfg, state, tr, ntfy, dry_run=args.dry_run, catch_up=args.catch_up)
        finally:
            state.close()

    seeded = report.seeded + report.seeded_completions
    log.info("done: %d added, %d finished, %d removed, %d errors%s", len(report.added),
             len(report.completed), len(report.removed), len(report.errors),
             f", {seeded} marked as already handled" if seeded else "")
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
