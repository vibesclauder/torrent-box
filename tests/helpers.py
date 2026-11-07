"""Shared test helpers: a fake nCore + Transmission HTTP server and config writers."""

import base64
import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rss2transmission  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"

# Temp dirs in RAM when available: every run writes the SQLite state with real fsyncs, which
# is slow on a spinning disk and pointless for tests.
if Path("/dev/shm").is_dir() and os.access("/dev/shm", os.W_OK):
    tempfile.tempdir = "/dev/shm"
PASSKEY = "SECRETPASSKEY123"


def fake_torrent(name):
    """Bytes that look enough like a .torrent for our purposes (starts with a bencoded dict)."""
    return f"d4:infod4:name{len(name)}:{name}ee".encode()


def make_feed(base_url, items):
    """items: list of (ncore_id, title), newest first like the real feed."""
    xml = ['<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>nCore</title>']
    for ncore_id, title in items:
        link = f"{base_url}/rss_dl.php/id={ncore_id}/key={PASSKEY}"
        xml.append(f"<item><title>{escape(title)}</title><link>{link}</link>"
                   f"<category>nCore</category></item>")
    xml.append("</channel></rss>")
    return "".join(xml).encode()


class FakeServer:
    """Serves /feed, /rss_dl.php/id=N/key=K (torrent downloads), /transmission/rpc and an
    ntfy JSON publish endpoint at POST /."""

    SESSION_ID = "fake-session-42"

    def __init__(self, rpc_user="user", rpc_password="pass"):
        self.feed = b""
        self.torrents = {}         # ncore_id -> (filename, bytes)
        self.added = {}            # info hash -> {"download-dir": ..., "metainfo": ...}
        self.tr_torrents = []      # what torrent-get returns; torrent-add appends here
        self.pushes = []           # ntfy messages received (decoded JSON)
        self.ntfy_down = False     # True -> ntfy answers 500
        self.rpc_auth = "Basic " + base64.b64encode(f"{rpc_user}:{rpc_password}".encode()).decode()
        self.rpc_down = False      # True -> RPC answers 503
        self.rpc_result = None     # torrent-add answers this, e.g. "invalid or corrupt torrent file"
        self.feed_status = 200
        self.requests = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, body=b"", headers=None):
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                server.requests.append(("GET", self.path))
                if self.path == "/feed":
                    return self._send(server.feed_status, server.feed,
                                      {"Content-Type": "application/rss+xml"})
                m = re.match(r"/rss_dl\.php/id=(\d+)/key=(\w+)$", self.path)
                if m and m.group(2) == PASSKEY and m.group(1) in server.torrents:
                    filename, data = server.torrents[m.group(1)]
                    return self._send(200, data, {
                        "Content-Type": "application/x-bittorrent",
                        "Content-Disposition": f'attachment; filename="{filename}"'})
                self._send(404, b"not found")

            def do_POST(self):
                server.requests.append(("POST", self.path))
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if self.path == "/":
                    if server.ntfy_down:
                        return self._send(500, b"ntfy down")
                    server.pushes.append(json.loads(body))
                    return self._send(200, b"{}")
                if self.path != "/transmission/rpc":
                    return self._send(404)
                if server.rpc_down:
                    return self._send(503, b"down")
                if self.headers.get("Authorization") != server.rpc_auth:
                    return self._send(401, b"Unauthorized")
                if self.headers.get("X-Transmission-Session-Id") != server.SESSION_ID:
                    return self._send(409, b"", {"X-Transmission-Session-Id": server.SESSION_ID})
                req = json.loads(body)
                if server.rpc_result and req["method"] == "torrent-add":
                    return self._send(200, json.dumps({"result": server.rpc_result}).encode())
                args = req.get("arguments", {})
                if req["method"] == "torrent-add":
                    metainfo = base64.b64decode(args["metainfo"])
                    info_hash = hashlib.sha1(metainfo).hexdigest()
                    key = "torrent-duplicate" if info_hash in server.added else "torrent-added"
                    if key == "torrent-added":
                        server.added[info_hash] = {"download-dir": args["download-dir"],
                                                   "metainfo": metainfo}
                        server.tr_torrents.append({
                            "hashString": info_hash, "name": f"torrent-{info_hash[:6]}",
                            "percentDone": 0.0, "downloadDir": args["download-dir"],
                            "sizeWhenDone": 1000})
                    reply = {key: {"hashString": info_hash, "id": 1, "name": "x"}}
                elif req["method"] == "torrent-get":
                    reply = {"torrents": server.tr_torrents}
                else:
                    reply = {}
                self._send(200, json.dumps({"result": "success", "arguments": reply}).encode())

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, args=(0.01,), daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()

    def set_items(self, items):
        """items: list of (ncore_id, category, name), newest first. Publishes feed + torrents."""
        self.feed = make_feed(self.base_url, [(i, name) for i, _, name in items])
        for ncore_id, category, name in items:
            self.torrents[ncore_id] = (f"[nCore][{category}]{name}.torrent", fake_torrent(name))

    def add_finished(self, name, size=4_500_000_000, download_dir="/mnt/storage/MOVIES/HD/HU"):
        """Put a torrent into Transmission's list that is already 100% done."""
        info_hash = hashlib.sha1(name.encode()).hexdigest()
        self.tr_torrents.append({"hashString": info_hash, "name": name, "percentDone": 1.0,
                                 "downloadDir": download_dir, "sizeWhenDone": size})
        return info_hash

    def finish_all(self):
        for t in self.tr_torrents:
            t["percentDone"] = 1.0

    def added_dirs(self):
        return sorted(v["download-dir"] for v in self.added.values())


# The [routes] section of the shipped example config, so tests exercise the real table.
_EXAMPLE = (Path(__file__).resolve().parents[1] / "config.example.ini").read_text()
ROUTES = _EXAMPLE[_EXAMPLE.index("\n[routes]\n") + len("\n[routes]\n"):]


def write_config(tmp, base_url, rpc_user="user", rpc_password="pass", require_mount="no"):
    """Write config.ini + secrets.ini into tmp (a Path); returns the config path."""
    (tmp / "storage").mkdir(exist_ok=True)
    (tmp / "watch").mkdir(exist_ok=True)
    config = tmp / "config.ini"
    config.write_text(f"""
[general]
storage_root      = {tmp / 'storage'}
require_mount     = {require_mount}
watch_dir         = {tmp / 'watch'}
archive_dir       = {tmp / 'archive'}
disk_warn_percent = 90

[transmission]
rpc_url = {base_url}/transmission/rpc
timeout = 5

[ntfy]
server = {base_url}

[routes]
{ROUTES}
""")
    (tmp / "secrets.ini").write_text(f"""
[secrets]
feed_url     = {base_url}/feed
rpc_user     = {rpc_user}
rpc_password = {rpc_password}
ntfy_topic   = test-topic
""")
    return config


OLD = [("101", "hd_hun", "Old.Movie.2001"), ("100", "xxx_hd", "Older.Thing")]


class RunCase(unittest.TestCase):
    """Fresh temp dir + fake server per test; run_once() does one full program run."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)
        self.server = FakeServer().__enter__()
        self.config_path = write_config(self.tmp, self.server.base_url)
        self.state_path = self.tmp / "state" / "state.sqlite"
        self.server.set_items(OLD)

    def tearDown(self):
        self.server.__exit__()
        self.tmpdir.cleanup()

    def run_once(self, dry_run=False, catch_up=False, topic=None, **cfg_changes):
        cfg = rss2transmission.load_config(self.config_path)
        for k, v in cfg_changes.items():
            setattr(cfg, k, v)
        state = rss2transmission.State(self.state_path, readonly=dry_run)
        tr = rss2transmission.Transmission(cfg.rpc_url, cfg.rpc_user, cfg.rpc_password, timeout=5)
        ntfy = rss2transmission.Ntfy(cfg.ntfy_server, cfg.ntfy_topic if topic is None else topic,
                                     timeout=5)
        try:
            return rss2transmission.run(cfg, state, tr, ntfy, dry_run=dry_run, catch_up=catch_up)
        finally:
            state.close()

    def storage(self, sub):
        return str(self.tmp / "storage" / sub)
