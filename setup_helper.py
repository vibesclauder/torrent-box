#!/usr/bin/env python3
"""setup_helper - the parts of setup.sh that are easier (and testable) in Python.

Used by setup.sh only; each subcommand does one small job:
  lan                     detect the LAN subnet from `ip -o -4 route show` on stdin
  merge-settings          merge our keys into Transmission's settings.json
  speed-test              measure the internet download speed, in Mbit/s
  arr-connect             connect Prowlarr, Sonarr, Radarr, Whisparr to Transmission, each other
  jellyfin-setup CONFIG   Jellyfin's first-run wizard: admin user and libraries
  jellyfin-login          does JF_USER / JF_PASSWORD work as a Jellyfin admin?
  jellyfin-hwaccel DEV    switch on hardware transcoding (vainfo's output on stdin), if off
  smartd-conf LINE        /etc/smartd.conf on stdin, with our storage disk line, to stdout
  seerr-setup             Seerr's first-run setup: Jellyfin sign-in, libraries, Sonarr, Radarr
  ncore-code              enter a fresh nCore 2FA code in Prowlarr (when its session expired)
  rpc-check URL           try a Transmission RPC call with RPC_USER / RPC_PASSWORD from env
  ini-get FILE SEC KEY    print one value from an INI file
  ini-set FILE SEC        set key=value lines (from stdin) in an INI file, atomically
  route-dirs CONFIG       storage root + every [routes] folder, parents first
  service-unit UNIT CFG   the rss2transmission service with ReadWritePaths= from config.ini
  render FILE VAR=VALUE   replace @VAR@ placeholders
  random-topic            a fresh ntfy topic
  tailscale-status        state, name and IPv4 from `tailscale status --json` on stdin
Secrets (passwords) are passed through the environment, never on the command line, and
never printed.
"""

import argparse
import configparser
import hashlib
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import string
import sys
import tempfile
import xml.etree.ElementTree as ET
import threading
import time
import http.cookiejar
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import rss2transmission

STATE_DIR = "~/.local/state/rss2transmission"
# Big files on fast servers near (central European) home, tried in order. Not Cloudflare's
# speed test: it caps a request at 50 MB and answers 429 to the many requests a fast line needs.
SPEED_TEST_URLS = ("https://nbg1-speed.hetzner.com/10GB.bin",
                   "https://fsn1-speed.hetzner.com/10GB.bin",
                   "https://proof.ovh.net/files/10Gb.dat")


### LAN subnet

def lan_subnet(route_output):
    """`ip -o -4 route show` output -> (network, our address) of the LAN, or None.

    The LAN is the directly connected network on the interface the default route uses."""
    routes = [line.split() for line in route_output.splitlines() if line.strip()]

    def after(words, key):
        return words[words.index(key) + 1] if key in words[:-1] else None

    default_dev = next((after(w, "dev") for w in routes if w[0] == "default"), None)
    candidates = []
    for w in routes:
        if w[0] == "default" or "/" not in w[0] or after(w, "scope") != "link":
            continue
        try:
            net = ipaddress.ip_network(w[0], strict=False)
        except ValueError:
            continue
        if net.is_loopback or net.is_link_local:
            continue
        candidates.append((after(w, "dev") != default_dev, net, after(w, "src")))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0])  # the default route's interface first
    _, net, src = candidates[0]
    return net, src


def whitelist_pattern(network):
    """192.168.1.0/24 -> '192.168.1.*'. Transmission's whitelist only knows whole-octet
    wildcards, so a prefix that isn't a multiple of 8 is widened (10.1.2.0/23 -> 10.1.*.*)."""
    network = ipaddress.ip_network(network, strict=False)
    if network.version != 4 or network.prefixlen < 8:
        raise ValueError(f"can't turn {network} into a Transmission whitelist entry")
    octets = str(network.network_address).split(".")
    keep = min(network.prefixlen // 8, 3)
    return ".".join(octets[:keep] + ["*"] * (4 - keep))


### Storage disk

# Never offered as the storage disk: not data filesystems, or parts of something bigger.
NOT_STORAGE_FS = {"swap", "linux_raid_member", "LVM2_member", "crypto_LUKS", "zfs_member",
                  "bcache", "iso9660", "udf", "squashfs"}
MIN_STORAGE_BYTES = 1 << 30


def human_size(n):
    """1234567890 -> '1.1G', like lsblk's (binary) sizes."""
    for unit in "BKMGTP":
        if n < 1024 or unit == "P":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024


def storage_candidates(lsblk_json):
    """`lsblk -J -b -o PATH,PKNAME,TYPE,SIZE,FSTYPE,PTTYPE,MOUNTPOINT,RO,UUID,LABEL,MODEL` ->
    [(path, kind, uuid, description)], biggest first: unmounted filesystems (kind "fs") and
    blank disks (kind "blank": no partitions, no partition table, no filesystem), which setup
    may partition and format. A disk with anything on it is never "blank"."""
    def flag(v):  # older lsblk writes "0"/"1" where newer writes false/true
        return v in (True, "1", 1)

    devs, parent = [], {}  # lsblk nests partitions under their disk, or lists them flat
    stack = [(d, None) for d in json.loads(lsblk_json).get("blockdevices", [])]
    while stack:
        dev, up = stack.pop()
        devs.append(dev)
        up = up or (f"/dev/{dev['pkname']}" if dev.get("pkname") else None)
        if up:
            parent[dev["path"]] = up
        stack.extend((kid, dev["path"]) for kid in dev.get("children") or [])
    by_path = {d["path"]: d for d in devs}
    has_kids = set(parent.values())

    found = []
    for dev in devs:
        size = int(dev.get("size") or 0)
        model = dev.get("model") or (by_path.get(parent.get(dev["path"])) or {}).get("model")
        fstype = dev.get("fstype")
        if (dev.get("type") in ("loop", "rom") or flag(dev.get("ro")) or dev.get("mountpoint")
                or size < MIN_STORAGE_BYTES
                or os.path.basename(dev["path"]).startswith(("zram", "loop"))):
            continue
        if fstype and dev.get("uuid") and fstype not in NOT_STORAGE_FS:
            what = " ".join(x for x in (dev.get("label"), model) if x)
            found.append((size, dev["path"], "fs", dev["uuid"],
                          f"{human_size(size)} {fstype}" + (f" {what}" if what else "")))
        elif (dev.get("type") == "disk" and dev["path"] not in has_kids and not fstype
              and not dev.get("pttype")):
            found.append((size, dev["path"], "blank", "",
                          f"{human_size(size)} blank" + (f" {model}" if model else "")
                          + ": partitioned and formatted ext4"))
    return [c[1:] for c in sorted(found, key=lambda c: (-c[0], c[1]))]


### Transmission settings.json

def fill(template, values):
    """Replace @NAME@ in every string of a JSON-like structure (single pass)."""
    def sub(text):
        def one(m):
            if m.group(1) not in values:
                raise KeyError(f"no value for @{m.group(1)}@")
            return values[m.group(1)]
        return re.sub(r"@([A-Z_]+)@", one, text)

    if isinstance(template, dict):
        return {k: fill(v, values) for k, v in template.items()}
    if isinstance(template, list):
        return [fill(v, values) for v in template]
    if isinstance(template, str):
        return sub(template)
    return template


def merge_settings(current, ours, keep_password=False):
    """Our keys on top of the existing settings; everything else is left alone.

    keep_password: the login is known to work, so leave the (hashed) rpc-password as is
    instead of writing the plain one again; Transmission would re-hash it with a new salt
    and the file would change on every run. Only honoured when authentication is already on
    for the same user (otherwise "works" means nothing).
    Returns (merged settings, sorted list of keys whose value changed)."""
    merged = dict(current)
    keep = (keep_password and current.get("rpc-authentication-required") is True
            and current.get("rpc-username") == ours.get("rpc-username")
            and "rpc-password" in current)
    for key, value in ours.items():
        if key == "rpc-password" and keep:
            continue
        merged[key] = value
    missing = object()
    changed = sorted(k for k in merged if current.get(k, missing) != merged[k])
    return merged, changed


def speed_settings(mbps, mem_mb):
    """Transmission settings sized for an internet download speed of mbps Mbit/s.

    cache-size-mb: about 2 s of download at full speed, so the randomly arriving pieces reach
    the disk as large writes instead of a seek per block (Transmission's default, 4 MB, is a
    fraction of a second at 1 Gbit/s); a power of two, 16..1024 MB, at most 1/16 of RAM.
    Peer limits: more connections are needed to fill a fast line."""
    want = max(mbps / 8 * 2, 1)
    cache = 1 << (int(want) - 1).bit_length() if want > 1 else 1
    cache = max(16, min(cache, 1024, mem_mb // 16))
    if mbps < 300:
        peers = (200, 50)  # Transmission's defaults
    elif mbps < 1000:
        peers = (400, 80)
    else:
        peers = (600, 120)
    return {"cache-size-mb": cache, "peer-limit-global": peers[0],
            "peer-limit-per-torrent": peers[1]}


def mem_total_mb(meminfo="/proc/meminfo"):
    with open(meminfo, encoding="ascii") as f:
        for line in f:
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) // 1024
    raise ValueError(f"no MemTotal in {meminfo}")


def cmd_merge_settings(args):
    try:
        with open(args.current, encoding="utf-8") as f:
            text = f.read()
        current = json.loads(text) if text.strip() else {}
    except FileNotFoundError:
        current = {}
    values = dict(v.split("=", 1) for v in args.var)
    values["RPC_PASSWORD"] = os.environ.get("RPC_PASSWORD", "")
    if not values["RPC_PASSWORD"]:
        sys.exit("merge-settings: RPC_PASSWORD is not set")
    ours = fill(json.loads(Path(args.template).read_text(encoding="utf-8")), values)
    if args.download_mbps:
        ours.update(speed_settings(args.download_mbps, mem_total_mb()))
    merged, changed = merge_settings(current, ours, args.keep_password)
    write_private(args.out, json.dumps(merged, indent=4, sort_keys=True, ensure_ascii=False) + "\n")
    for key in changed:
        print(key)


### Internet speed

def speed_test(urls=SPEED_TEST_URLS, **kwargs):
    """Download speed in Mbit/s from the first server that works. Raises OSError if none."""
    errors = []
    for url in urls:
        try:
            return measure_download(url, **kwargs)
        except OSError as e:
            errors.append(f"{url}: {e}")
    raise OSError("speed test failed: " + "; ".join(errors))


def measure_download(url, streams=8, warmup=2.0, seconds=6.0):
    """Download speed in Mbit/s: several parallel downloads of url (one TCP stream rarely
    fills a fast line), counting only the bytes after a warm-up. OSError if nothing came."""
    received = [0] * streams
    stop = threading.Event()
    errors = []

    def worker(i):
        req = urllib.request.Request(url, headers={"User-Agent": "torrent-box-setup"})
        while not stop.is_set():
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    while not stop.is_set():
                        chunk = resp.read1(1 << 16)  # what has arrived: counted as it comes
                        if not chunk:
                            break
                        received[i] += len(chunk)
            except OSError as e:
                errors.append(e)
                return

    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(streams)]
    for t in threads:
        t.start()
    time.sleep(warmup)
    if len(errors) == streams:  # every download failed: don't wait for nothing
        raise OSError(str(errors[0]))
    start, before = time.monotonic(), sum(received)
    time.sleep(seconds)
    got, elapsed = sum(received) - before, time.monotonic() - start
    stop.set()
    for t in threads:
        t.join(timeout=2)
    if got <= 0:
        raise OSError(str(errors[0]) if errors else "no data")
    return round(got * 8 / elapsed / 1e6)


### Prowlarr, Sonarr, Radarr, Whisparr

# name: (port, API version). They run with host networking, so localhost reaches them.
ARR_APPS = {"prowlarr": (9696, "v1"), "sonarr": (8989, "v3"), "radarr": (7878, "v3"),
            "whisparr": (6969, "v3")}
BASE_APPS = ("prowlarr", "sonarr", "radarr")  # with arr = yes; Whisparr with whisparr = yes too
# The download client's per-app category/folder fields: tvCategory/tvDirectory and so on.
CLIENT_PREFIX = {"sonarr": "tv", "radarr": "movie"}
CLIENT_NAME = "Transmission"  # the download client we add and keep up to date
MASKED = "********"           # how the APIs return passwords and keys
STATE_FILE = "torrent-box-state.json"  # in the arr dir: what arr-connect has done already


def load_state(arr_dir):
    """What earlier runs did (the APIs can't tell: passwords come back masked)."""
    try:
        state = json.loads((Path(arr_dir) / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def save_state(arr_dir, state):
    write_private(Path(arr_dir) / STATE_FILE, json.dumps(state, indent=2, sort_keys=True) + "\n")


def login_fingerprint(user, password, salt=None):
    """A salted hash of the login, to tell later whether it changed without storing it."""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", f"{user}:{password}".encode(), salt.encode(), 100_000)
    return {"salt": salt, "hash": digest.hex()}


def login_changed(state, user, password):
    """Is this another login than the one the apps were last given (or none yet)?"""
    known = state.get("login") or {}
    return not known.get("salt") or login_fingerprint(user, password, known["salt"]) != known


def arr_api_key(config_xml):
    """The API key from an app's config.xml, or None while it hasn't written one yet."""
    try:
        key = ET.parse(config_xml).getroot().findtext("ApiKey")
    except (OSError, ET.ParseError):
        return None
    return key.strip() if key and key.strip() else None


class ArrApi:
    def __init__(self, name, api_key, timeout=30):
        port, self.version = ARR_APPS[name]
        self.name, self.base, self.api_key, self.timeout = name, f"http://localhost:{port}", \
            api_key, timeout

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{self.base}/api/{self.version}/{path}", data=data, method=method,
            headers={"X-Api-Key": self.api_key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                text = resp.read().decode()
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            raise OSError(f"{self.name}: {method} {path}: HTTP {e.code}: {detail}") from None
        return json.loads(text) if text.strip() else None

    def wait(self, seconds=120):
        """Wait until the API answers (the apps take a while to start)."""
        deadline = time.monotonic() + seconds
        while True:
            try:
                return self.call("GET", "system/status")
            except OSError:
                if time.monotonic() > deadline:
                    raise OSError(f"{self.name} doesn't answer on {self.base}") from None
                time.sleep(2)


def set_fields(item, values):
    """Set item["fields"] by name; every name has to exist (the schema changed otherwise)."""
    fields = {f["name"]: f for f in item["fields"]}
    missing = sorted(set(values) - set(fields))
    if missing:
        raise ValueError(f"{item.get('implementation')}: no field(s) {', '.join(missing)}")
    for name, value in values.items():
        fields[name]["value"] = value


def fields_differ(item, values):
    """Does item have other values than these? Masked secrets can't be compared: skipped."""
    current = {f["name"]: f.get("value") for f in item["fields"]}
    return any(current.get(k) != v for k, v in values.items() if current.get(k) != MASKED)


def ensure_item(api, kind, name, implementation, values, extra=None, force=False):
    """Make sure the download client / application `name` exists with these field values.
    Other settings (and other items) are left alone. force: PUT even if nothing seems to
    differ (a masked password can't be compared). Returns "added", "updated" or "ok"."""
    extra = extra or {}
    existing = next((i for i in api.call("GET", kind) if i.get("name") == name), None)
    if existing is None:
        template = next((t for t in api.call("GET", f"{kind}/schema")
                         if t.get("implementation") == implementation), None)
        if template is None:
            raise ValueError(f"{api.name}: no {implementation} in {kind}/schema")
        item = dict(template, name=name, enable=True, **extra)
        set_fields(item, values)
        api.call("POST", kind, item)
        return "added"
    if not force and not fields_differ(existing, values) and \
            all(existing.get(k) == v for k, v in extra.items()):
        return "ok"
    item = dict(existing, **extra)
    set_fields(item, values)
    api.call("PUT", f"{kind}/{existing['id']}", item)
    return "updated"


def remove_item(api, kind, name):
    """Delete the download client / application `name`, if there is one. Returns whether."""
    item = next((i for i in api.call("GET", kind) if i.get("name") == name), None)
    if item is not None:
        api.call("DELETE", f"{kind}/{item['id']}")
    return item is not None


def client_prefix(api):
    """The prefix of the app's Transmission category/folder fields ("tv" in Sonarr). Whisparr's
    follow the app its version was forked from (Sonarr for v2, Radarr for v3): read from its
    schema, the field pair <prefix>Category + <prefix>Directory."""
    if api.name in CLIENT_PREFIX:
        return CLIENT_PREFIX[api.name]
    template = next((t for t in api.call("GET", "downloadclient/schema")
                     if t.get("implementation") == "Transmission"), None)
    names = {f["name"] for f in (template or {}).get("fields", [])}
    prefixes = sorted(n.removesuffix("Directory") for n in names if n.endswith("Directory"))
    prefix = next((p for p in prefixes if p + "Category" in names), None)
    if prefix is None:
        raise ValueError(f"{api.name}: no Transmission category/directory fields in its schema")
    return prefix


def ensure_root_folders(api, paths):
    have = {f["path"].rstrip("/") for f in api.call("GET", "rootfolder")}
    added = []
    for path in paths:
        if path.rstrip("/") not in have:
            api.call("POST", "rootfolder", {"path": path})
            added.append(path)
    return added


def ensure_login(api, user, password, force=False):
    """The app's own login page (forms) with this user, for every address (the apps reach each
    other with API keys, which don't need it). Not "disabled for local addresses": that needs
    an Allowed Hosts list of every name the box is reached by. The password can't be read back
    (masked or hashed), so an app that already has forms login for this user is left alone,
    unless force (the Transmission login changed since the last run)."""
    host = api.call("GET", "config/host")
    if not force and host.get("authenticationMethod") == "forms" and host.get("username") == user:
        return "ok"
    host.update(authenticationMethod="forms", authenticationRequired="enabled",
                username=user, password=password, passwordConfirmation=password)
    api.call("PUT", "config/host/1", host)
    return "set"


HUN_FORMAT = {"name": "HUN", "includeCustomFormatWhenRenaming": False, "specifications": [
    {"name": "HUN in the release name", "implementation": "ReleaseTitleSpecification",
     "negate": False, "required": False, "fields": [{"name": "value", "value": r"\bHUN\b"}]}]}
HUN_SCORE = 100


def ensure_hun_format(api, state):
    """A "HUN" custom format (Hungarian releases), scored up in every quality profile, so a
    Hungarian release wins over an otherwise equal one. Done once per app (state "hun_scored"):
    scores you change later are kept. Until then (a run stopped halfway), profiles where HUN
    still scores 0 get the score."""
    done = state.setdefault("hun_scored", [])
    fmt = next((f for f in api.call("GET", "customformat") if f.get("name") == HUN_FORMAT["name"]),
               None)
    if fmt is not None and api.name in done:
        return "ok"
    result = "ok"
    if fmt is None:
        fmt, result = api.call("POST", "customformat", HUN_FORMAT), "added"
    for profile in api.call("GET", "qualityprofile"):
        items = profile.get("formatItems") or []
        item = next((i for i in items if i.get("format") == fmt["id"]), None)
        if item is None:  # normally added (score 0) to every profile by the app itself
            item = {"format": fmt["id"], "name": fmt["name"], "score": 0}
            items.insert(0, item)
        if item.get("score"):  # already scored (by an earlier run, or by you)
            continue
        item["score"] = HUN_SCORE
        profile["formatItems"] = items
        api.call("PUT", f"qualityprofile/{profile['id']}", profile)
        result = "added" if result == "added" else "scored"
    done.append(api.name)
    return result


ANY_LANGUAGE = {"id": -1, "name": "Any"}


def ensure_any_language(api, state):
    """Radarr's quality profiles want the movie's original language by default, so every
    Hungarian release of an English film is rejected ("Original Language (English) is wanted,
    but found Hungarian"). Any language instead; the HUN format still ranks Hungarian first.
    Once per app (state "language_any"), so a language you pick later is kept. Sonarr's
    profiles have no language (Sonarr does that with custom formats): "ok"."""
    done = state.setdefault("language_any", [])
    if api.name in done:
        return "ok"
    result = "ok"
    for profile in api.call("GET", "qualityprofile"):
        if "language" not in profile or profile["language"].get("id") == ANY_LANGUAGE["id"]:
            continue
        profile["language"] = dict(ANY_LANGUAGE)
        api.call("PUT", f"qualityprofile/{profile['id']}", profile)
        result = "set to Any"
    done.append(api.name)
    return result


def find_ncore(prowlarr):
    return next((i for i in prowlarr.call("GET", "indexer") if i.get("definitionName") == "ncore"),
                None)


def ncore_status(prowlarr, state):
    """ "present", "missing", or "off": added, but never switched on (the first login failed),
    so it still needs a 2FA code. Switched off by you after it worked counts as present."""
    indexer = find_ncore(prowlarr)
    if indexer is None:
        return "missing"
    return "present" if indexer.get("enable") or state.get("ncore_on") else "off"


def ensure_ncore(prowlarr, user, password, code, state):
    """The nCore indexer (Cardigann definition "ncore"). Added once; an existing one that
    worked is never touched, since saving it again would log in with an old 2FA code. One
    whose first login failed is switched on with this code. No seed limits: Transmission
    keeps seeding, which meets nCore's rule (ratio 1.0 or 48 h + 0.4 h/GB).

    Added switched off, then switched on with the code: Prowlarr only keeps the session
    cookies of a login made for a saved indexer (one with an id), so logging in while adding
    it would throw the "stay logged in" session away, and the next search would log in again
    with the by then expired code."""
    status = ncore_status(prowlarr, state)
    if status == "present":
        state["ncore_on"] = True
        return "ok"
    if status == "off":
        ncore_login(prowlarr, code)
        state["ncore_on"] = True
        return "switched on"
    template = next((t for t in prowlarr.call("GET", "indexer/schema")
                     if t.get("definitionName") == "ncore"), None)
    if template is None:
        raise ValueError("prowlarr: no nCore in indexer/schema")
    profiles = prowlarr.call("GET", "appprofile")
    if not profiles:
        raise ValueError("prowlarr: no app profile")
    item = dict(template, name="nCore", enable=False, appProfileId=profiles[0]["id"],
                priority=25)
    set_fields(item, {"username": user, "password": password, "2facode": ""})
    prowlarr.call("POST", "indexer", item)  # disabled: no login yet
    ncore_login(prowlarr, code)
    state["ncore_on"] = True
    return "added"


def ncore_login(prowlarr, code):
    """Switch the nCore indexer on with this 2FA code: Prowlarr logs in (and keeps the
    session), then sends the indexer to Sonarr and Radarr. Also how a new code is entered
    after nCore's session expired (`setup_helper.py ncore-code`)."""
    indexer = find_ncore(prowlarr)
    if indexer is None:
        raise ValueError("prowlarr: no nCore indexer")
    indexer["enable"] = True
    set_fields(indexer, {"2facode": code})
    prowlarr.call("PUT", f"indexer/{indexer['id']}", indexer)  # tests it: the login
    prowlarr.call("POST", "command", {"name": "ApplicationIndexerSync", "forceSync": True})


def transmission_fields(user, password):
    return {"host": "localhost", "port": 9091, "useSsl": False, "urlBase": "/transmission/",
            "username": user, "password": password}


def arr_connect(apis, keys, rpc_user, rpc_password, downloads_dir, misc_dir, series_roots,
                movie_roots, ncore=None, ntfy=None, state=None, xxx_roots=()):
    """Connect the apps. apis: name -> ArrApi (Whisparr is connected if it's in there, and
    taken out of Prowlarr if not); keys: name -> API key; ncore: (user, password, 2FA code)
    for Prowlarr's indexer; ntfy: (server, topic) for Prowlarr's health alerts; state: what
    earlier runs did (load_state), updated as it goes. Yields report lines."""
    state = {} if state is None else state
    tr = transmission_fields(rpc_user, rpc_password)
    # The apps return the Transmission password masked, so a changed one can't be seen in
    # them: compared with the last run's instead, and then everything that has it is re-saved.
    new_login = login_changed(state, rpc_user, rpc_password)
    if ncore:  # first: a 2FA code is only valid for a moment
        yield f"prowlarr: nCore indexer: {ensure_ncore(apis['prowlarr'], *ncore, state)}"
    for name in (n for n in ARR_APPS if n in apis):
        result = ensure_login(apis[name], rpc_user, rpc_password, force=new_login)
        yield f"{name}: login {rpc_user}: {result}"
    # Sonarr, Radarr and Whisparr: their torrents go to <downloads>/series, /movies and /xxx,
    # then get hardlinked into the library. A directory, not a category: they can't have both,
    # and each one takes the torrents in its directory as its own, which is what a category
    # is for.
    libraries = [("sonarr", "series", series_roots), ("radarr", "movies", movie_roots)]
    if "whisparr" in apis:
        libraries.append(("whisparr", "xxx", xxx_roots))
    for name, sub, roots in libraries:
        prefix = client_prefix(apis[name])
        result = ensure_item(apis[name], "downloadclient", CLIENT_NAME, "Transmission", dict(
            tr, **{f"{prefix}Category": "", f"{prefix}Directory": f"{downloads_dir}/{sub}"}),
            # the torrent keeps seeding after the import (hardlinked, so it costs nothing)
            extra={"removeCompletedDownloads": False}, force=new_login)
        yield f"{name}: download client {CLIENT_NAME}: {result}"
        for path in ensure_root_folders(apis[name], roots):
            yield f"{name}: root folder added: {path}"
        yield f"{name}: custom format HUN: {ensure_hun_format(apis[name], state)}"
        yield f"{name}: profile language: {ensure_any_language(apis[name], state)}"
    # Prowlarr: grabs from its own search page go to the catch-all folder, like hand-added
    # torrents; the others get the indexers synced to them (Whisparr: nCore's XXX categories).
    result = ensure_item(apis["prowlarr"], "downloadclient", CLIENT_NAME, "Transmission",
                         dict(tr, category="", directory=misc_dir), force=new_login)
    yield f"prowlarr: download client {CLIENT_NAME}: {result}"
    for name, _, _ in libraries:
        result = ensure_item(apis["prowlarr"], "applications", name.capitalize(),
                             name.capitalize(), {
                                 "prowlarrUrl": apis["prowlarr"].base, "baseUrl": apis[name].base,
                                 "apiKey": keys[name]}, extra={"syncLevel": "fullSync"})
        yield f"prowlarr: application {name.capitalize()}: {result}"
    # Whisparr switched off (its container is gone): Prowlarr would keep reporting it down
    if "whisparr" not in apis and remove_item(apis["prowlarr"], "applications", "Whisparr"):
        yield "prowlarr: application Whisparr: removed"
    if ntfy:
        # e.g. the nCore login failing once its session expires (new 2FA code needed)
        result = ensure_item(apis["prowlarr"], "notification", "ntfy (torrent-box)", "Ntfy",
                             {"serverUrl": ntfy[0], "topics": [ntfy[1]], "priority": 4},
                             extra={"onHealthIssue": True, "onHealthRestored": True,
                                    "includeHealthWarnings": False})
        yield f"prowlarr: health alerts to ntfy: {result}"
        # Torrents Sonarr, Radarr or Prowlarr's search page grab (all through Prowlarr): the
        # "Added" push for them, as rss2transmission sends for its own. Low priority, like
        # those; its own item since a notification has one priority.
        result = ensure_item(apis["prowlarr"], "notification", "ntfy grabs (torrent-box)", "Ntfy",
                             {"serverUrl": ntfy[0], "topics": [ntfy[1]], "priority": 2,
                              "tags": ["arrow_down"]},
                             extra={"onGrab": True, "includeManualGrabs": True,
                                    "onHealthIssue": False, "onHealthRestored": False})
        yield f"prowlarr: grabs to ntfy: {result}"
    state["login"] = login_fingerprint(rpc_user, rpc_password)  # only once all have it


def wait_arr_apps(arr_dir, seconds=180, names=BASE_APPS):
    """Wait until these apps answer; returns (keys, apis)."""
    keys, apis = {}, {}
    deadline = time.monotonic() + seconds
    for name in names:
        # config.xml (with the key) appears a few seconds after a container's first start
        while (key := arr_api_key(Path(arr_dir) / name / "config.xml")) is None:
            if time.monotonic() > deadline:
                raise OSError(f"{name}: no API key in {arr_dir}/{name}/config.xml")
            time.sleep(2)
        keys[name], apis[name] = key, ArrApi(name, key)
        apis[name].wait(max(deadline - time.monotonic(), 10))
    return keys, apis


def cmd_arr_connect(args):
    keys, apis = wait_arr_apps(args.arr_dir,
                               names=BASE_APPS + (("whisparr",) if args.whisparr else ()))
    rpc_user, rpc_password = os.environ.get("RPC_USER", ""), os.environ.get("RPC_PASSWORD", "")
    if not rpc_user or not rpc_password:
        sys.exit("arr-connect: RPC_USER / RPC_PASSWORD are not set")
    ncore = None
    if os.environ.get("NCORE_USER") and os.environ.get("NCORE_PASSWORD"):
        ncore = (os.environ["NCORE_USER"], os.environ["NCORE_PASSWORD"],
                 os.environ.get("NCORE_2FA", ""))
    ntfy = (args.ntfy_server, args.ntfy_topic) if args.ntfy_topic else None
    state = load_state(args.arr_dir)
    try:
        for line in arr_connect(apis, keys, rpc_user, rpc_password, args.downloads_dir,
                                args.misc_dir, args.series_root, args.movie_root, ncore, ntfy,
                                state, args.xxx_root):
            print(line)
    finally:  # also what got done before an error, so the next run doesn't redo it
        save_state(args.arr_dir, state)


def cmd_ncore_code(args):
    """Ask for a fresh 2FA code and log Prowlarr in to nCore with it."""
    _, apis = wait_arr_apps(args.arr_dir, 30)
    code = input("nCore 2FA code from your authenticator app: ").strip()
    ncore_login(apis["prowlarr"], code)
    state = load_state(args.arr_dir)
    state["ncore_on"] = True
    save_state(args.arr_dir, state)
    print("nCore: logged in; indexer sent to Sonarr and Radarr")


def cmd_seerr_status(args):
    """Prints "initialized" or "new"."""
    public = wait_for(f"{SEERR}/api/v1/settings/public")
    print("initialized" if public.get("initialized") else "new")


def whisparr_stashdb_key(arr_dir):
    """The API key of Whisparr's StashDB import list, or "" if it has none.

    Read from its database: its API hides the key ("********"). Read-only, and a busy or
    missing database just means no key."""
    path = Path(arr_dir) / "whisparr" / "whisparr3.db"
    if not path.exists():
        return ""
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            rows = db.execute("SELECT Settings FROM ImportLists WHERE Implementation LIKE 'StashDB%' "
                              "ORDER BY Enabled DESC, Id").fetchall()
        finally:
            db.close()
    except sqlite3.Error:
        return ""
    for (settings,) in rows:
        try:
            values = {k.lower(): v for k, v in json.loads(settings or "{}").items()}
        except (ValueError, AttributeError):
            continue
        key = str(values.get("apikey") or "").strip()
        if key:
            return key
    return ""


def cmd_whisparr_stashdb_key(args):
    """Prints the key (for setup.sh to hand to Desireerr), or nothing."""
    print(whisparr_stashdb_key(args.arr_dir))


def cmd_ncore_status(args):
    """Prints ncore_status(): "present", "missing" or "off" (the last two need a 2FA code).
    Waits for all the apps first, so a code asked for next is used right away (it's only
    valid briefly)."""
    _, apis = wait_arr_apps(args.arr_dir)
    print(ncore_status(apis["prowlarr"], load_state(args.arr_dir)))


### Jellyfin

JELLYFIN = "http://localhost:8096"
JF_CLIENT = 'MediaBrowser Client="torrent-box-setup", Device="setup", DeviceId="torrent-box-setup", Version="1.0"'

# (library, collection type, [routes] codes, metadata language, country): README's layout
JELLYFIN_LIBRARIES = [
    ("Filmek", "movies", ("hd_hun", "xvid_hun"), "hu", "HU"),
    ("Movies", "movies", ("hd", "xvid"), "en", "US"),
    ("Sorozatok", "tvshows", ("hdser_hun", "xvidser_hun"), "hu", "HU"),
    ("Series", "tvshows", ("hdser", "xvidser"), "en", "US"),
    ("Music", "music", ("mp3_hun", "mp3", "lossless_hun", "lossless"), "", ""),
]


def http_json(method, url, body=None, headers=None, opener=None, timeout=30):
    """A JSON request; returns the decoded answer (None if empty). HTTP errors -> OSError
    with the status and the start of the body."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with (opener or urllib.request.build_opener()).open(req, timeout=timeout) as resp:
            text = resp.read().decode()
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        err = OSError(f"{method} {url}: HTTP {e.code}: {detail}")
        err.status = e.code
        raise err from None
    return json.loads(text) if text.strip() else None


def wait_for(url, seconds=120):
    deadline = time.monotonic() + seconds
    while True:
        try:
            return http_json("GET", url)
        except OSError:
            if time.monotonic() > deadline:
                raise OSError(f"no answer from {url}") from None
            time.sleep(2)


def jellyfin_libraries(config_path):
    """README's library layout, with the folders from config.ini's [routes] (only those
    that are routed; Music is the top folder of the music routes)."""
    conf = rss2transmission._read_ini(config_path)
    root = Path(os.path.expanduser(conf.get("general", "storage_root").strip()))
    routes = dict(conf.items("routes"))
    out = []
    for name, ctype, codes, lang, country in JELLYFIN_LIBRARIES:
        rels = [routes[c].strip().strip("/") for c in codes if routes.get(c, "").strip()]
        if ctype == "music":
            rels = [r.split("/")[0] for r in rels]
        paths = list(dict.fromkeys(str(root / r) for r in rels))
        if paths:
            out.append((name, ctype, paths, lang, country))
    return out


def jellyfin_auth(user, password, base=JELLYFIN):
    """Log in; returns (token, is_admin). OSError (status 401) if refused."""
    res = http_json("POST", f"{base}/Users/AuthenticateByName", {"Username": user, "Pw": password},
                    headers={"Authorization": JF_CLIENT})
    policy = (res.get("User") or {}).get("Policy") or {}
    return res["AccessToken"], bool(policy.get("IsAdministrator"))


def jellyfin_setup(config_path, user, password, base=JELLYFIN):
    """Run the first-run wizard if it hasn't been: admin = user/password, the libraries.
    Yields report lines. Leaves a set-up Jellyfin alone."""
    info = wait_for(f"{base}/System/Info/Public")
    if info.get("StartupWizardCompleted"):
        yield "already set up (users and libraries are left as they are)"
        return
    http_json("POST", f"{base}/Startup/Configuration", {
        "UICulture": "en-US", "MetadataCountryCode": "HU", "PreferredMetadataLanguage": "hu"})
    http_json("GET", f"{base}/Startup/User")  # creates the first user; the POST needs it
    try:
        http_json("POST", f"{base}/Startup/User", {"Name": user, "Password": password})
        yield f"admin user: {user}"
    except OSError as e:
        if getattr(e, "status", None) != 403:  # 403: the first user already has a password
            raise
        yield "admin user: already created"
    http_json("POST", f"{base}/Startup/RemoteAccess", {"EnableRemoteAccess": True})
    existing = {f["Name"] for f in http_json("GET", f"{base}/Library/VirtualFolders") or []}
    for name, ctype, paths, lang, country in jellyfin_libraries(config_path):
        if name in existing:  # a duplicate name would silently become "Movies2"
            continue
        query = urllib.parse.urlencode({"name": name, "collectionType": ctype,
                                        "paths": ",".join(paths), "refreshLibrary": "true"})
        options = {"EnableRealtimeMonitor": True}
        if lang:
            options.update(PreferredMetadataLanguage=lang, MetadataCountryCode=country)
        http_json("POST", f"{base}/Library/VirtualFolders?{query}", {"LibraryOptions": options})
        yield f"library {name} ({ctype}): {', '.join(paths)}"
    http_json("POST", f"{base}/Startup/Complete")
    yield "first-run wizard completed"


### Jellyfin: hardware transcoding

# vainfo profile -> Jellyfin's name for the codec (its Dashboard -> Playback -> Transcoding)
VAAPI_DECODE = [("VAProfileH264", "h264"), ("VAProfileHEVCMain", "hevc"),
                ("VAProfileMPEG2", "mpeg2video"), ("VAProfileVC1", "vc1"),
                ("VAProfileVP8", "vp8"), ("VAProfileVP9Profile0", "vp9"),
                ("VAProfileAV1Profile0", "av1")]


def vaapi_caps(vainfo):
    """`vainfo` output -> (Jellyfin decode codecs, decodes 10-bit HEVC, 10-bit VP9, encodes
    H.264)."""
    entries = re.findall(r"^\s*(VAProfile\w+)\s*:\s*(VAEntrypoint\w+)", vainfo, re.M)
    decode = {p for p, e in entries if e == "VAEntrypointVLD"}
    encode = {p for p, e in entries if e.startswith("VAEntrypointEnc")}
    codecs = [name for prefix, name in VAAPI_DECODE
              if any(p.startswith(prefix) for p in decode)]
    return (codecs, "VAProfileHEVCMain10" in decode, "VAProfileVP9Profile2" in decode,
            any(p.startswith("VAProfileH264") for p in encode))


def jellyfin_hwaccel(user, password, device, vainfo, base=JELLYFIN):
    """Switch on VAAPI transcoding with what the GPU can do, if Jellyfin's hardware
    acceleration is still off; a choice made in Jellyfin is left alone. Returns a line."""
    codecs, hevc10, vp9_10, h264_encode = vaapi_caps(vainfo)
    if "h264" not in codecs:
        return "transcoding: the GPU doesn't decode H.264 through VAAPI; left on the CPU"
    token, admin = jellyfin_auth(user, password, base)
    if not admin:
        raise OSError(f"{user} is not a Jellyfin admin")
    auth = {"Authorization": f'{JF_CLIENT}, Token="{token}"'}
    options = http_json("GET", f"{base}/System/Configuration/encoding", headers=auth)
    current = options.get("HardwareAccelerationType") or "none"
    if current.lower() != "none":
        return f"transcoding: hardware acceleration already set ({current}), left as is"
    options.update(HardwareAccelerationType="vaapi", VaapiDevice=device,
                   HardwareDecodingCodecs=codecs, EnableDecodingColorDepth10Hevc=hevc10,
                   EnableDecodingColorDepth10Vp9=vp9_10, EnableHardwareEncoding=h264_encode)
    http_json("POST", f"{base}/System/Configuration/encoding", options, headers=auth)
    return (f"transcoding: hardware (VAAPI, {device}), decodes {', '.join(codecs)}"
            + (", encodes H.264" if h264_encode else ""))


### smartd

SMARTD_BEGIN = "# torrent-box: the storage disk (setup.sh keeps this block up to date)"
SMARTD_END = "# torrent-box end"


def smartd_conf(text, line):
    """smartd.conf with our block (one device line) in it: replaced if it's there, else put
    before the first DEVICESCAN, which then skips that disk (same identity), or at the end."""
    block = [SMARTD_BEGIN, line, SMARTD_END]
    lines = text.splitlines()
    if SMARTD_BEGIN in lines:
        start = lines.index(SMARTD_BEGIN)
        end = lines.index(SMARTD_END, start) if SMARTD_END in lines[start:] else start
        lines[start:end + 1] = block
    else:
        at = next((i for i, l in enumerate(lines) if l.split()[:1] == ["DEVICESCAN"]),
                  len(lines))
        lines[at:at] = block + [""]
    return "\n".join(lines) + "\n"


### Seerr

SEERR = "http://localhost:5055"


class Seerr:
    """Seerr's API: the session cookie of the admin's sign-in, or the API key later."""

    def __init__(self, base=SEERR, api_key=None):
        self.base, self.api_key = base, api_key
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, method, path, body=None):
        headers = {"X-Api-Key": self.api_key} if self.api_key else {}
        return http_json(method, f"{self.base}/api/v1/{path}", body, headers, self.opener)


def seerr_api_key(settings_json):
    try:
        return json.loads(Path(settings_json).read_text())["main"]["apiKey"] or None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def pick_profile(profiles):
    """The profile Seerr requests with: HD-1080p if there is one, else the first."""
    return next((p for p in profiles if "1080" in p["name"]), profiles[0])


def seerr_arr(seerr, kind, port, api_key, root, **extra):
    """Make sure Seerr has the Sonarr/Radarr on localhost:port. Returns "added" or "ok"."""
    if any(a.get("port") == port and a.get("hostname") in ("localhost", "127.0.0.1")
           for a in seerr.call("GET", f"settings/{kind}") or []):
        return "ok"
    conn = {"hostname": "localhost", "port": port, "apiKey": api_key, "useSsl": False,
            "baseUrl": ""}
    found = seerr.call("POST", f"settings/{kind}/test", conn)
    if not found.get("profiles"):
        raise OSError(f"{kind} has no quality profiles")
    profile = pick_profile(found["profiles"])
    seerr.call("POST", f"settings/{kind}", dict(
        conn, name=kind.capitalize(), activeProfileId=profile["id"],
        activeProfileName=profile["name"], activeDirectory=root, is4k=False, isDefault=True,
        externalUrl="", syncEnabled=False, preventSearch=False, tagRequests=False, tags=[],
        **extra))
    return "added"


# Seerr's notification types, a bit mask (its server/lib/notifications/index.ts)
SEERR_PENDING, SEERR_FAILED, SEERR_AUTO_APPROVED, SEERR_ISSUE = 2, 16, 128, 256
# Pushed: a request (waiting for your approval, or approved by itself), a failed one, an
# issue someone reported. Not "available": rss2transmission's "Downloaded" push covers that.
SEERR_PUSHES = SEERR_PENDING | SEERR_AUTO_APPROVED | SEERR_FAILED | SEERR_ISSUE


def seerr_ntfy(seerr, server, topic):
    """Seerr's ntfy notifications to our topic. Set up once (an agent without a topic); later
    runs only keep server and topic current, so the types you pick in Seerr stay, and one
    you switched off stays off. Returns "added", "updated", "ok" or "off"."""
    agent = seerr.call("GET", "settings/notifications/ntfy") or {}
    options = dict(agent.get("options") or {})
    if not options.get("topic"):
        result, agent = "added", dict(agent, enabled=True, types=SEERR_PUSHES)
    elif not agent.get("enabled"):
        return "off"
    elif (options.get("url"), options.get("topic")) == (server, topic):
        return "ok"
    else:
        result = "updated"
    options.update(url=server, topic=topic)
    seerr.call("POST", "settings/notifications/ntfy", dict(agent, options=options))
    return result


def seerr_app_url(seerr, url):
    """Seerr's application URL (its links in notifications), if it has none yet."""
    main = seerr.call("GET", "settings/main") or {}
    if main.get("applicationUrl"):
        return "ok"
    main = {k: v for k, v in main.items() if k != "apiKey"}  # read-only: a 400 otherwise
    seerr.call("POST", "settings/main", dict(main, applicationUrl=url))
    return "set"


def seerr_setup(settings_json, jf_user, jf_password, arr_keys, series_root, movie_root,
                ntfy=None, app_url="", base=SEERR):
    """Seerr's first-run setup (sign in with the Jellyfin admin, which makes it Seerr's admin;
    enable the movie and show libraries; finish), then Sonarr and Radarr, the application
    URL and the ntfy pushes (ntfy: (server, topic)). On later runs only the last parts, with
    the API key. Yields report lines."""
    public = wait_for(f"{base}/api/v1/settings/public")
    key = seerr_api_key(settings_json)
    if public.get("initialized") and key:
        seerr = Seerr(base, key)
    else:
        seerr = Seerr(base)
        if public.get("mediaServerType") == 2:  # Jellyfin already set: only sign in
            seerr.call("POST", "auth/jellyfin", {"username": jf_user, "password": jf_password})
        else:
            seerr.call("POST", "auth/jellyfin", {
                "username": jf_user, "password": jf_password, "hostname": "localhost",
                "port": 8096, "useSsl": False, "urlBase": "",
                "email": f"{jf_user}@{os.uname().nodename}.local", "serverType": 2})
            yield f"signed in with Jellyfin as {jf_user} (Seerr's admin)"
    if not public.get("initialized"):
        libraries = seerr.call("POST", "settings/jellyfin/library/sync") or []
        for lib in libraries:
            if not lib.get("enabled"):
                seerr.call("PUT", f"settings/jellyfin/library/{lib['id']}", {"enabled": True})
        yield f"libraries: {', '.join(l['name'] for l in libraries) or 'none'}"
    if arr_keys.get("sonarr") and series_root:
        yield "sonarr: " + seerr_arr(
            seerr, "sonarr", ARR_APPS["sonarr"][0], arr_keys["sonarr"], series_root,
            seriesType="standard", animeSeriesType="anime", animeTags=[],
            enableSeasonFolders=True, monitorNewItems="all")
    if arr_keys.get("radarr") and movie_root:
        yield "radarr: " + seerr_arr(
            seerr, "radarr", ARR_APPS["radarr"][0], arr_keys["radarr"], movie_root,
            minimumAvailability="released")
    if app_url:
        yield f"application URL ({app_url}): {seerr_app_url(seerr, app_url)}"
    if ntfy:
        yield f"requests and issues to ntfy: {seerr_ntfy(seerr, *ntfy)}"
    if not public.get("initialized"):
        seerr.call("POST", "settings/initialize")
        yield "setup finished"


### Files

def write_private(path, text, mode=0o600):
    """Write a file atomically; it never exists, even briefly, with looser permissions."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")  # mkstemp: 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def set_ini_values(text, section, values):
    """Set key = value in [section] of INI text, keeping comments, order and other keys.
    Existing keys are changed in place, new ones go after the section's last key."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip() == f"[{section}]"), None)
    if start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"[{section}]")
        start = len(lines) - 1
    end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")),
               len(lines))
    width = 0
    last_key = start
    found = {}
    for i in range(start + 1, end):
        m = re.match(r"\s*([^#;\s][^=:]*?)(\s*)[=:]", lines[i])
        if m:
            found[m.group(1)] = i
            width = max(width, len(m.group(1) + m.group(2)))
            last_key = i
    for key, value in values.items():
        if "\n" in value or "\r" in value:
            raise ValueError(f"{key}: value can't contain a line break")
        line = f"{key.ljust(max(width, len(key) + 1))}= {value}"  # line up with the others
        if key in found:
            lines[found[key]] = line
        else:
            last_key += 1
            lines.insert(last_key, line)
    return "\n".join(lines) + "\n"


def cmd_ini_set(args):
    values = {}
    for line in sys.stdin.read().splitlines():
        if line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    path = Path(args.file)
    if path.exists():
        text, mode = path.read_text(encoding="utf-8"), path.stat().st_mode & 0o777
    else:
        text, mode = "", int(args.mode, 8)
    write_private(path, set_ini_values(text, args.section, values), mode)


def cmd_ini_get(args):
    try:
        value = rss2transmission._read_ini(args.file).get(args.section, args.key)
    except (rss2transmission.ConfigError, configparser.Error):
        return 1
    print(os.path.expanduser(value.strip()) if args.path else value.strip())
    return 0


### Config-derived paths

def route_dirs(config_path):
    """Storage root and every routed folder (with all their parents), parents first."""
    conf = rss2transmission._read_ini(config_path)
    root = Path(os.path.expanduser(conf.get("general", "storage_root").strip()))
    dirs = {root}
    for _, folder in conf.items("routes"):
        rel = Path(folder.strip().strip("/"))
        if not str(rel) or rel.is_absolute() or ".." in rel.parts:
            raise ValueError(f"bad folder in [routes]: {folder!r}")
        for i in range(1, len(rel.parts) + 1):
            dirs.add(root.joinpath(*rel.parts[:i]))
    return sorted(dirs, key=lambda p: (len(p.parts), str(p)))


def unit_path(path, home):
    """A path as written in a unit file: %h for the home dir, % escaped, quoted if needed."""
    path, home = str(path), str(home).rstrip("/")
    prefix = ""
    if path == home or path.startswith(home + "/"):
        prefix, path = "%h", path[len(home):]
    path = prefix + path.replace("%", "%%")
    if re.search(r'[\s"\\]', path):
        path = '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return path


def rewrite_read_write_paths(unit_text, paths, home):
    """Replace the ReadWritePaths= line of a unit with the given directories."""
    words = []
    for p in paths:
        word = unit_path(p, home)
        if word not in words:
            words.append(word)
    new, count = re.subn(r"(?m)^ReadWritePaths=.*$",
                         lambda _: "ReadWritePaths=" + " ".join(words), unit_text)
    if count != 1:
        raise ValueError("expected exactly one ReadWritePaths= line in the unit")
    return new


def service_paths(config_path):
    conf = rss2transmission._read_ini(config_path)
    return [os.path.expanduser(STATE_DIR)] + [
        os.path.expanduser(conf.get("general", key).strip()) for key in ("watch_dir", "archive_dir")]


### Misc

def render(text, values, strip_comments=False):
    for name, value in values.items():
        text = text.replace(f"@{name}@", value)
    left = re.findall(r"@[A-Z_]+@", text)
    if left:
        raise KeyError(f"no value for {', '.join(sorted(set(left)))}")
    if strip_comments:
        text = "\n".join(l for l in text.splitlines() if not l.startswith("#")).lstrip("\n") + "\n"
    return text


def random_topic(length=16):
    alphabet = string.ascii_lowercase + string.digits
    return "torrent-box-" + "".join(secrets.choice(alphabet) for _ in range(length))


def tailscale_status(text):
    """`tailscale status --json` -> (backend state, MagicDNS name, Tailscale IPv4).

    State is e.g. "Running", "NeedsLogin", "Stopped"; "" if there's nothing to parse
    (not installed, tailscaled not running). Missing name/address -> ""."""
    try:
        status = json.loads(text)
    except ValueError:
        return "", "", ""
    if not isinstance(status, dict):
        return "", "", ""
    me = status.get("Self") or {}
    ipv4 = next((ip for ip in me.get("TailscaleIPs") or [] if "." in ip), "")
    return status.get("BackendState") or "", (me.get("DNSName") or "").rstrip("."), ipv4


def rpc_check(url, user, password, timeout=10):
    """Returns (exit code, message): 0 works, 3 login refused / not allowed, 4 unreachable."""
    tr = rss2transmission.Transmission(url, user, password, timeout)
    try:
        tr.call("session-get")
    except rss2transmission.TransmissionUnavailable as e:
        cause = e.__cause__
        if isinstance(cause, urllib.error.HTTPError) and cause.code in (401, 403):
            return 3, ("login refused" if cause.code == 401
                       else "access denied (not in rpc-whitelist?)")
        return 4, f"not reachable: {e}"
    except rss2transmission.TorrentRejected as e:
        return 4, f"unexpected answer: {e}"
    return 0, "login works"


### Main

def main(argv=None):
    ap = argparse.ArgumentParser(description="helper for setup.sh")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("lan")
    p.add_argument("--subnet", help="use this subnet instead of detecting it")
    p = sub.add_parser("merge-settings")
    p.add_argument("--template", required=True)
    p.add_argument("--current", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--var", action="append", default=[], help="NAME=VALUE for @NAME@")
    p.add_argument("--keep-password", action="store_true")
    p.add_argument("--download-mbps", type=int, default=0,
                   help="internet download speed: also set the cache and peer limits")
    sub.add_parser("speed-test")
    p = sub.add_parser("jellyfin-setup")
    p.add_argument("config")
    sub.add_parser("jellyfin-login")
    p = sub.add_parser("jellyfin-hwaccel")
    p.add_argument("device")
    p = sub.add_parser("smartd-conf")
    p.add_argument("line")
    p = sub.add_parser("seerr-setup")
    p.add_argument("--arr-dir", required=True)
    p.add_argument("--series-root", default="")
    p.add_argument("--movie-root", default="")
    p.add_argument("--app-url", default="", help="Seerr's address, for links in its pushes")
    p.add_argument("--ntfy-server", default="https://ntfy.sh")
    p.add_argument("--ntfy-topic", default="")
    p = sub.add_parser("arr-connect")
    p.add_argument("--arr-dir", required=True, help="where the apps keep their config")
    p.add_argument("--downloads-dir", required=True)
    p.add_argument("--misc-dir", required=True)
    p.add_argument("--series-root", action="append", default=[])
    p.add_argument("--movie-root", action="append", default=[])
    p.add_argument("--whisparr", action="store_true", help="connect Whisparr too")
    p.add_argument("--xxx-root", action="append", default=[], help="Whisparr's root folders")
    p.add_argument("--ntfy-server", default="https://ntfy.sh")
    p.add_argument("--ntfy-topic", default="")
    sub.add_parser("seerr-status")
    p = sub.add_parser("ncore-code", help="enter a fresh nCore 2FA code in Prowlarr")
    p.add_argument("--arr-dir", default=os.path.expanduser("~/.local/share/torrent-box/arr"))
    p = sub.add_parser("ncore-status")
    p.add_argument("--arr-dir", required=True)
    p = sub.add_parser("whisparr-stashdb-key", help="the key of Whisparr's StashDB import list")
    p.add_argument("--arr-dir", required=True)
    p = sub.add_parser("rpc-check")
    p.add_argument("url")
    p = sub.add_parser("ini-get")
    p.add_argument("--path", action="store_true", help="expand ~")
    p.add_argument("file")
    p.add_argument("section")
    p.add_argument("key")
    p = sub.add_parser("ini-set")
    p.add_argument("--mode", default="600", help="octal mode for a new file")
    p.add_argument("file")
    p.add_argument("section")
    p = sub.add_parser("route-dirs")
    p.add_argument("config")
    p = sub.add_parser("service-unit")
    p.add_argument("unit")
    p.add_argument("config")
    p = sub.add_parser("render")
    p.add_argument("--strip-comments", action="store_true")
    p.add_argument("file")
    p.add_argument("var", nargs="*", help="NAME=VALUE")
    sub.add_parser("random-topic")
    sub.add_parser("tailscale-status")
    sub.add_parser("storage-candidates", help="lsblk -J on stdin -> tab-separated candidates")
    args = ap.parse_args(argv)

    try:
        if args.cmd == "lan":
            found = lan_subnet(sys.stdin.read())
            if args.subnet:  # given by the user; our address only if it's in there
                net = ipaddress.ip_network(args.subnet, strict=False)
                src = found[1] if found and found[1] and \
                    ipaddress.ip_address(found[1]) in net else ""
            elif found:
                net, src = found
            else:
                print("no LAN subnet found", file=sys.stderr)
                return 1
            print(net, whitelist_pattern(net), src or "")
        elif args.cmd == "merge-settings":
            cmd_merge_settings(args)
        elif args.cmd == "jellyfin-setup":
            for line in jellyfin_setup(args.config, os.environ.get("JF_USER", ""),
                                       os.environ.get("JF_PASSWORD", "")):
                print(line)
        elif args.cmd == "jellyfin-hwaccel":
            print(jellyfin_hwaccel(os.environ.get("JF_USER", ""), os.environ.get("JF_PASSWORD", ""),
                                   args.device, sys.stdin.read()))
        elif args.cmd == "smartd-conf":
            sys.stdout.write(smartd_conf(sys.stdin.read(), args.line))
        elif args.cmd == "jellyfin-login":
            try:
                _, admin = jellyfin_auth(os.environ.get("JF_USER", ""),
                                         os.environ.get("JF_PASSWORD", ""))
            except OSError as e:
                print("refused" if getattr(e, "status", None) == 401 else f"error: {e}")
                return 3
            print("admin" if admin else "not an admin")
            return 0 if admin else 3
        elif args.cmd == "seerr-setup":
            keys = {n: arr_api_key(Path(args.arr_dir) / n / "config.xml") for n in ("sonarr", "radarr")}
            for line in seerr_setup(Path(args.arr_dir) / "seerr" / "settings.json",
                                    os.environ.get("JF_USER", ""), os.environ.get("JF_PASSWORD", ""),
                                    keys, args.series_root, args.movie_root,
                                    (args.ntfy_server, args.ntfy_topic) if args.ntfy_topic else None,
                                    args.app_url):
                print(line)
        elif args.cmd == "arr-connect":
            cmd_arr_connect(args)
        elif args.cmd == "ncore-code":
            cmd_ncore_code(args)
        elif args.cmd == "seerr-status":
            cmd_seerr_status(args)
        elif args.cmd == "ncore-status":
            cmd_ncore_status(args)
        elif args.cmd == "whisparr-stashdb-key":
            cmd_whisparr_stashdb_key(args)
        elif args.cmd == "speed-test":
            print(speed_test())
        elif args.cmd == "rpc-check":
            code, message = rpc_check(args.url, os.environ.get("RPC_USER", ""),
                                      os.environ.get("RPC_PASSWORD", ""))
            print(message)
            return code
        elif args.cmd == "ini-get":
            return cmd_ini_get(args)
        elif args.cmd == "ini-set":
            cmd_ini_set(args)
        elif args.cmd == "route-dirs":
            for d in route_dirs(args.config):
                print(d)
        elif args.cmd == "service-unit":
            text = Path(args.unit).read_text(encoding="utf-8")
            print(rewrite_read_write_paths(text, service_paths(args.config),
                                           Path.home()), end="")
        elif args.cmd == "render":
            values = dict(v.split("=", 1) for v in args.var)
            print(render(Path(args.file).read_text(encoding="utf-8"), values,
                         args.strip_comments), end="")
        elif args.cmd == "random-topic":
            print(random_topic())
        elif args.cmd == "tailscale-status":
            print(" ".join(v or "-" for v in tailscale_status(sys.stdin.read())))
        elif args.cmd == "storage-candidates":
            for c in storage_candidates(sys.stdin.read()):
                print("\t".join(c))
    except (OSError, ValueError, KeyError, configparser.Error,
            rss2transmission.ConfigError) as e:
        print(f"setup_helper {args.cmd}: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
