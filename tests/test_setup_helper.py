import contextlib
import io
import ipaddress
import json
import os
import re
import socket
import stat
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from helpers import FakeServer

import setup_helper as sh

REPO = Path(__file__).resolve().parents[1]

# `ip -o -4 route show` on a real box, plus a docker bridge for good measure.
ROUTES = """\
default via 192.168.1.1 dev enp3s0 proto dhcp metric 100
169.254.0.0/16 dev enp3s0 scope link metric 1000
172.17.0.0/16 dev docker0 proto kernel scope link src 172.17.0.1 linkdown
192.168.1.0/24 dev enp3s0 proto kernel scope link src 192.168.1.50 metric 100
"""


class LanTest(unittest.TestCase):
    def test_default_route_interface_wins(self):
        net, src = sh.lan_subnet(ROUTES)
        self.assertEqual((str(net), src), ("192.168.1.0/24", "192.168.1.50"))

    def test_no_default_route(self):
        net, _ = sh.lan_subnet("10.0.5.0/24 dev eth0 proto kernel scope link src 10.0.5.9\n")
        self.assertEqual(str(net), "10.0.5.0/24")

    def test_nothing_usable(self):
        self.assertIsNone(sh.lan_subnet("169.254.0.0/16 dev eth0 scope link metric 1000\n"))
        self.assertIsNone(sh.lan_subnet(""))

    def test_whitelist_pattern(self):
        self.assertEqual(sh.whitelist_pattern("192.168.1.0/24"), "192.168.1.*")
        self.assertEqual(sh.whitelist_pattern("172.16.0.0/16"), "172.16.*.*")
        self.assertEqual(sh.whitelist_pattern("10.0.0.0/8"), "10.*.*.*")
        self.assertEqual(sh.whitelist_pattern("10.1.2.0/23"), "10.1.*.*")  # widened
        self.assertEqual(sh.whitelist_pattern("192.168.1.128/25"), "192.168.1.*")
        with self.assertRaises(ValueError):
            sh.whitelist_pattern("0.0.0.0/0")

    def test_cli(self):
        self.assertEqual(run_main(["lan"], stdin=ROUTES), (0, "192.168.1.0/24 192.168.1.* 192.168.1.50\n"))
        self.assertEqual(run_main(["lan", "--subnet", "10.0.0.0/16"], stdin=ROUTES),
                         (0, "10.0.0.0/16 10.0.*.* \n"))
        self.assertEqual(run_main(["lan"], stdin="")[0], 1)


class SettingsTest(unittest.TestCase):
    VALUES = {"RPC_USER": "alice", "RPC_PASSWORD": "s3cret", "DOWNLOAD_DIR": "/mnt/storage/MISC",
              "INCOMPLETE_DIR": "/mnt/storage/.incomplete", "RPC_WHITELIST": "192.168.1.*",
              "HOSTNAME": "box"}
    # A few keys from Debian's stock settings.json
    DEBIAN = {"rpc-authentication-required": True, "rpc-username": "transmission",
              "rpc-password": "{62b16db87b89a91dd49a5110a7cafc06d20eb4f2wtK6kqPj",
              "rpc-whitelist": "127.0.0.1", "download-dir": "/var/lib/transmission-daemon/downloads",
              "speed-limit-up": 100, "speed-limit-up-enabled": False, "umask": 18}

    def ours(self):
        template = json.loads((REPO / "templates/transmission-settings.json").read_text())
        return sh.fill(template, self.VALUES)

    def test_template_filled(self):
        ours = self.ours()
        self.assertEqual(ours["rpc-whitelist"], "127.0.0.1,192.168.1.*")
        # http://box:9091/, http://box.local:9091/, http://box.tailnet.ts.net:9091/ (not just IPs)
        self.assertEqual(ours["rpc-host-whitelist"], "box,box.*")
        self.assertIs(ours["rpc-host-whitelist-enabled"], True)
        self.assertEqual(ours["download-dir"], "/mnt/storage/MISC")
        # partial downloads on the same disk: finishing one is a rename, not a copy
        self.assertEqual(ours["incomplete-dir"], "/mnt/storage/.incomplete")
        self.assertIs(ours["incomplete-dir-enabled"], True)
        self.assertEqual(ours["preallocation"], 2)  # full: files don't fragment on the HDD
        self.assertEqual(ours["rpc-password"], "s3cret")
        self.assertEqual(ours["rpc-port"], 9091)
        self.assertIs(ours["script-torrent-done-enabled"], False)
        # nCore's torrents are private anyway: a public one added by hand stays out of the swarm
        for key in ("dht-enabled", "pex-enabled", "lpd-enabled"):
            self.assertIs(ours[key], False, key)
        self.assertFalse(re.search(r"@[A-Z_]+@", json.dumps(ours)))

    def test_fill_single_pass_and_unknown(self):
        self.assertEqual(sh.fill({"a": "@X@"}, {"X": "@Y@", "Y": "no"}), {"a": "@Y@"})
        with self.assertRaises(KeyError):
            sh.fill({"a": "@NOPE@"}, {})

    def test_merge_keeps_unrelated_and_sets_ours(self):
        merged, changed = sh.merge_settings(self.DEBIAN, self.ours())
        self.assertEqual(merged["speed-limit-up"], 100)
        self.assertIs(merged["speed-limit-up-enabled"], False)
        for key, value in self.ours().items():
            self.assertEqual(merged[key], value)
        self.assertIn("rpc-password", changed)
        self.assertIn("umask", changed)
        self.assertNotIn("speed-limit-up", changed)
        self.assertNotIn("rpc-authentication-required", changed)  # was already true

    def test_merge_is_idempotent(self):
        once, _ = sh.merge_settings(self.DEBIAN, self.ours())
        twice, changed = sh.merge_settings(once, self.ours())
        self.assertEqual(once, twice)
        self.assertEqual(changed, [])

    def test_keep_password_when_login_works(self):
        # After Transmission hashed our password, a re-run with a working login changes nothing.
        hashed = dict(sh.merge_settings(self.DEBIAN, self.ours())[0], **{"rpc-password": "{hash"})
        merged, changed = sh.merge_settings(hashed, self.ours(), keep_password=True)
        self.assertEqual(merged["rpc-password"], "{hash")
        self.assertEqual(changed, [])
        # without keep_password the plain password is written again
        _, changed = sh.merge_settings(hashed, self.ours())
        self.assertEqual(changed, ["rpc-password"])

    def test_keep_password_ignored_if_it_proves_nothing(self):
        no_auth = dict(self.DEBIAN, **{"rpc-authentication-required": False, "rpc-username": "alice"})
        merged, _ = sh.merge_settings(no_auth, self.ours(), keep_password=True)
        self.assertEqual(merged["rpc-password"], "s3cret")
        other_user = dict(self.DEBIAN)  # user "transmission", we want "alice"
        merged, _ = sh.merge_settings(other_user, self.ours(), keep_password=True)
        self.assertEqual(merged["rpc-password"], "s3cret")

    def test_cli_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "current.json").write_text(json.dumps(self.DEBIAN))
            argv = ["merge-settings", "--template", str(REPO / "templates/transmission-settings.json"),
                    "--current", str(tmp / "current.json"), "--out", str(tmp / "new.json"),
                    "--var", "RPC_USER=alice", "--var", "DOWNLOAD_DIR=/mnt/storage/MISC",
                    "--var", "INCOMPLETE_DIR=/mnt/storage/.incomplete",
                    "--var", "RPC_WHITELIST=192.168.1.*", "--var", "HOSTNAME=box"]
            with mock.patch.dict(os.environ, {"RPC_PASSWORD": "s3cret"}):
                code, out = run_main(argv)
            self.assertEqual(code, 0)
            self.assertNotIn("s3cret", out)  # only key names are printed
            self.assertIn("rpc-username", out.split())
            self.assertEqual(stat.S_IMODE((tmp / "new.json").stat().st_mode), 0o600)
            new = json.loads((tmp / "new.json").read_text())
            self.assertEqual((new["rpc-username"], new["speed-limit-up"]), ("alice", 100))
            # merging the result again: nothing to change
            (tmp / "new.json").rename(tmp / "current.json")
            with mock.patch.dict(os.environ, {"RPC_PASSWORD": "s3cret"}):
                self.assertEqual(run_main(argv), (0, ""))
            # a missing settings.json counts as empty
            (tmp / "current.json").unlink()
            with mock.patch.dict(os.environ, {"RPC_PASSWORD": "s3cret"}):
                code, out = run_main(argv)
            self.assertEqual(code, 0)
            self.assertIn("rpc-password", out.split())
            # without a speed the cache and peer limits are left alone
            new = json.loads((tmp / "new.json").read_text())
            self.assertNotIn("cache-size-mb", new)
            # with one they're set, sized for it and the box's RAM
            with mock.patch.dict(os.environ, {"RPC_PASSWORD": "s3cret"}), \
                    mock.patch.object(sh, "mem_total_mb", return_value=12000):
                code, out = run_main(argv + ["--download-mbps", "2000"])
            self.assertEqual(code, 0)
            new = json.loads((tmp / "new.json").read_text())
            self.assertEqual((new["cache-size-mb"], new["peer-limit-global"],
                              new["peer-limit-per-torrent"]), (512, 600, 120))

    def test_speed_settings(self):
        def cache(mbps, mem_mb=16000):
            return sh.speed_settings(mbps, mem_mb)["cache-size-mb"]
        self.assertEqual(cache(2000), 512)  # 250 MB/s: 2 s = 500 MB, next power of two
        self.assertEqual(cache(1000), 256)
        self.assertEqual(cache(100), 32)
        self.assertEqual(cache(10), 16)     # floor
        self.assertEqual(cache(10000, mem_mb=32000), 1024)  # ceiling
        self.assertEqual(cache(2000, mem_mb=2000), 125)  # at most 1/16 of RAM
        self.assertEqual(cache(2000, mem_mb=100), 16)    # but never below the floor
        peers = lambda mbps: tuple(sh.speed_settings(mbps, 16000)[k] for k in
                                   ("peer-limit-global", "peer-limit-per-torrent"))
        self.assertEqual(peers(100), (200, 50))  # Transmission's defaults
        self.assertEqual(peers(500), (400, 80))
        self.assertEqual(peers(1000), (600, 120))

    def test_mem_total_mb(self):
        with tempfile.NamedTemporaryFile("w", suffix=".meminfo") as f:
            f.write("MemTotal:       13547008 kB\nMemFree:  1 kB\n")
            f.flush()
            self.assertEqual(sh.mem_total_mb(f.name), 13229)


class IniTest(unittest.TestCase):
    def test_set_existing_and_new_keys(self):
        text = "# comment\n[secrets]\nfeed_url     = old\nrpc_user     = bob\n\n[other]\nx = 1\n"
        new = sh.set_ini_values(text, "secrets", {"feed_url": "https://x/y=z", "ntfy_topic": "t"})
        self.assertEqual(new, "# comment\n[secrets]\nfeed_url     = https://x/y=z\n"
                              "rpc_user     = bob\nntfy_topic   = t\n\n[other]\nx = 1\n")

    def test_new_section_and_empty_file(self):
        self.assertEqual(sh.set_ini_values("", "secrets", {"a": "1"}), "[secrets]\na = 1\n")
        self.assertEqual(sh.set_ini_values("[x]\nb = 2\n", "secrets", {"a": "1"}),
                         "[x]\nb = 2\n\n[secrets]\na = 1\n")

    def test_commented_key_is_not_replaced(self):
        new = sh.set_ini_values("[general]\n# storage_root = /old\nstorage_root = /mnt/storage\n",
                                "general", {"storage_root": "/srv/media"})
        self.assertIn("# storage_root = /old\nstorage_root = /srv/media\n", new)

    def test_rejects_line_breaks(self):
        with self.assertRaises(ValueError):
            sh.set_ini_values("", "s", {"a": "1\n[evil]"})

    def test_example_config_storage_root(self):
        text = (REPO / "config.example.ini").read_text()
        new = sh.set_ini_values(text, "general", {"storage_root": "/srv/media"})
        self.assertIn("storage_root      = /srv/media\n", new)
        self.assertEqual(len(new.splitlines()), len(text.splitlines()))

    def test_example_config_setup_choices(self):
        # empty in the example (setup.sh asks), then set in place by setup.sh
        text = (REPO / "config.example.ini").read_text()
        self.assertEqual(run_main(["ini-get", str(REPO / "config.example.ini"), "setup",
                                   "media_server"]), (0, "\n"))
        self.assertEqual(run_main(["ini-get", str(REPO / "config.example.ini"), "setup",
                                   "samba"]), (0, "\n"))
        self.assertEqual(run_main(["ini-get", str(REPO / "config.example.ini"), "setup",
                                   "tailscale"]), (0, "\n"))
        self.assertEqual(run_main(["ini-get", str(REPO / "config.example.ini"), "setup",
                                   "download_mbps"]), (0, "\n"))
        self.assertEqual(run_main(["ini-get", str(REPO / "config.example.ini"), "setup",
                                   "arr"]), (0, "\n"))
        self.assertEqual(run_main(["ini-get", str(REPO / "config.example.ini"), "setup",
                                   "arr_auto_update"]), (0, "\n"))
        new = sh.set_ini_values(text, "setup", {"media_server": "jellyfin", "samba": "yes",
                                                "tailscale": "no", "download_mbps": "2000"})
        self.assertTrue(new.endswith("\ndownload_mbps   = 2000\n"))
        self.assertIn("\nmedia_server    = jellyfin\n", new)
        self.assertIn("\nsamba           = yes\n", new)
        self.assertIn("\ntailscale       = no\n", new)
        self.assertEqual(len(new.splitlines()), len(text.splitlines()))

    def test_media_server_added_to_old_config(self):
        new = sh.set_ini_values("[general]\nstorage_root = /mnt/storage\n", "setup",
                                {"media_server": "minidlna"})
        self.assertEqual(new, "[general]\nstorage_root = /mnt/storage\n\n[setup]\n"
                              "media_server = minidlna\n")

    def test_cli_set_and_get(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "secrets.ini"
            code, _ = run_main(["ini-set", str(path), "secrets"],
                               stdin="feed_url=https://ncore.pro/bookmarks/abc\nrpc_password=p=w d\n")
            self.assertEqual(code, 0)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(run_main(["ini-get", str(path), "secrets", "rpc_password"]), (0, "p=w d\n"))
            self.assertEqual(run_main(["ini-get", str(path), "secrets", "nope"]), (1, ""))
            self.assertEqual(run_main(["ini-get", str(path) + ".missing", "secrets", "x"]), (1, ""))
            # an existing file keeps its mode
            path.chmod(0o640)
            run_main(["ini-set", str(path), "secrets"], stdin="ntfy_topic=t\n")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
            self.assertEqual(run_main(["ini-get", str(path), "secrets", "feed_url"]),
                             (0, "https://ncore.pro/bookmarks/abc\n"))
            self.assertEqual(sorted(os.listdir(tmp)), ["secrets.ini"])  # no temp files left

    def test_ini_get_expands_paths(self):
        code, out = run_main(["ini-get", "--path", str(REPO / "config.example.ini"),
                              "general", "watch_dir"])
        self.assertEqual((code, out), (0, str(Path.home() / "torrents") + "\n"))


class PathsTest(unittest.TestCase):
    def test_route_dirs(self):
        dirs = sh.route_dirs(REPO / "config.example.ini")
        root = Path("/mnt/storage")
        self.assertEqual(dirs[0], root)
        for d in ("MISC", "MOVIES", "MOVIES/HD", "MOVIES/HD/HU", "XXX/IMG", "GAMES/CNS"):
            self.assertIn(root / d, dirs)
        self.assertEqual(len(dirs), len(set(dirs)))
        for d in dirs[1:]:  # parents come before their children
            self.assertLess(dirs.index(d.parent), dirs.index(d))

    def test_route_dirs_rejects_escapes(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.ini"
            config.write_text("[general]\nstorage_root = /mnt/s\n[routes]\nhd = ../etc\n")
            with self.assertRaises(ValueError):
                sh.route_dirs(config)

    def test_unit_path(self):
        home = "/home/u"
        self.assertEqual(sh.unit_path("/home/u/torrents", home), "%h/torrents")
        self.assertEqual(sh.unit_path("/home/user2/x", home), "/home/user2/x")
        self.assertEqual(sh.unit_path("/srv/100%", home), "/srv/100%%")
        self.assertEqual(sh.unit_path("/home/u/my torrents", home), '"%h/my torrents"')

    def test_rewrite_read_write_paths(self):
        unit = "[Service]\nPrivateUsers=yes\nReadWritePaths=%h/old\nNice=10\n"
        new = sh.rewrite_read_write_paths(
            unit, ["/home/u/.local/state/rss2transmission", "/srv/torrents", "/srv/torrents"], "/home/u")
        self.assertEqual(new, "[Service]\nPrivateUsers=yes\n"
                              "ReadWritePaths=%h/.local/state/rss2transmission /srv/torrents\nNice=10\n")
        with self.assertRaises(ValueError):
            sh.rewrite_read_write_paths("[Service]\n", ["/x"], "/home/u")

    def test_shipped_unit_unchanged_with_default_config(self):
        # The default watch/archive dirs give back exactly the unit in the repo.
        text = (REPO / "systemd/rss2transmission.service").read_text()
        paths = sh.service_paths(REPO / "config.example.ini")
        self.assertEqual(sh.rewrite_read_write_paths(text, paths, Path.home()), text)
        self.assertIn("PrivateUsers=yes", text)

    def test_service_unit_cli_uses_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.ini"
            config.write_text("[general]\nwatch_dir = /srv/watch\narchive_dir = ~/archive\n")
            code, out = run_main(["service-unit", str(REPO / "systemd/rss2transmission.service"),
                                  str(config)])
        self.assertEqual(code, 0)
        self.assertIn("\nReadWritePaths=%h/.local/state/rss2transmission /srv/watch %h/archive\n", out)


class MiscTest(unittest.TestCase):
    def test_render_drop_in(self):
        out = sh.render((REPO / "systemd/storage-override.conf").read_text(),
                        {"STORAGE_ROOT": "/mnt/storage"}, strip_comments=True)
        self.assertTrue(out.startswith("[Unit]\nRequiresMountsFor=/mnt/storage\n"))
        self.assertNotIn("#", out)
        self.assertTrue(out.endswith("RestartSec=30\n"))

    def test_render_minidlna(self):
        out = sh.render((REPO / "templates/minidlna.conf").read_text(),
                        {"STORAGE_ROOT": "/mnt/storage", "HOSTNAME": "box"})
        self.assertIn("\nmedia_dir=/mnt/storage\n", out)
        self.assertIn("\nfriendly_name=box\n", out)
        self.assertIn("\ninotify=yes\n", out)
        self.assertEqual(out.count("album_art_names="), 3)

    def test_render_jellyfin_sources(self):
        out = sh.render((REPO / "templates/jellyfin.sources").read_text(),
                        {"URL": "https://repo.jellyfin.org/ubuntu", "SUITE": "resolute",
                         "ARCH": "amd64", "KEY": "/etc/apt/keyrings/jellyfin.asc"})
        stanza = [l for l in out.splitlines() if not l.startswith("#")]
        self.assertEqual(stanza, ["Types: deb", "URIs: https://repo.jellyfin.org/ubuntu",
                                  "Suites: resolute", "Components: main", "Architectures: amd64",
                                  "Signed-By: /etc/apt/keyrings/jellyfin.asc"])

    def test_render_samba(self):
        out = sh.render((REPO / "templates/samba.conf").read_text(),
                        {"STORAGE_ROOT": "/mnt/storage", "USER": "me",
                         "HOSTS_ALLOW": "127.0.0.1 ::1 192.168.1.0/24 fe80::/10"})
        share = [l.strip() for l in out.splitlines() if l.strip() and not l.lstrip().startswith("#")]
        self.assertEqual(share[0], "[storage]")  # nothing global: it's included into smb.conf
        self.assertIn("path = /mnt/storage", share)
        self.assertIn("valid users = me", share)
        self.assertIn("hosts allow = 127.0.0.1 ::1 192.168.1.0/24 fe80::/10", share)
        self.assertIn("hosts deny = ALL", share)

    def test_render_missing_value(self):
        with self.assertRaises(KeyError):
            sh.render("x=@STORAGE_ROOT@ @HOSTNAME@", {"STORAGE_ROOT": "/s"})

    def test_tailscale_status(self):
        running = json.dumps({"BackendState": "Running", "Self": {
            "DNSName": "box.tail1234.ts.net.", "TailscaleIPs": ["fd7a:115c:a1e0::1", "100.101.102.103"]}})
        self.assertEqual(sh.tailscale_status(running),
                         ("Running", "box.tail1234.ts.net", "100.101.102.103"))
        self.assertEqual(sh.tailscale_status('{"BackendState": "NeedsLogin", "Self": null}'),
                         ("NeedsLogin", "", ""))
        self.assertEqual(sh.tailscale_status(""), ("", "", ""))  # not installed / not running
        self.assertEqual(sh.tailscale_status("[]"), ("", "", ""))
        self.assertEqual(run_main(["tailscale-status"], stdin=running),
                         (0, "Running box.tail1234.ts.net 100.101.102.103\n"))
        self.assertEqual(run_main(["tailscale-status"], stdin=""), (0, "- - -\n"))

    def test_random_topic(self):
        topic = sh.random_topic()
        self.assertRegex(topic, r"^torrent-box-[a-z0-9]{16}$")
        self.assertNotEqual(topic, sh.random_topic())


class _Zeros(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", str(64 << 20))
        self.end_headers()
        chunk = bytes(1 << 16)
        try:
            for _ in range(1024):
                self.wfile.write(chunk)
        except OSError:  # the client stopped reading
            pass

    def log_message(self, *args):
        pass


class _Trickle(BaseHTTPRequestHandler):
    """A slow line: 16 KiB every 20 ms (about 6.5 Mbit/s)."""
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", str(64 << 20))
        self.end_headers()
        try:
            for _ in range(1000):
                self.wfile.write(bytes(1 << 14))
                self.wfile.flush()
                time.sleep(0.02)
        except OSError:
            pass

    def log_message(self, *args):
        pass


def closed_port_url():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{s.getsockname()[1]}/big"


class SpeedTestTest(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Zeros)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}/big"

    def test_measures(self):
        mbps = sh.measure_download(self.url, streams=2, warmup=0.2, seconds=0.3)
        self.assertGreater(mbps, 0)

    def test_falls_back_to_the_next_server(self):
        mbps = sh.speed_test([closed_port_url(), self.url], streams=2, warmup=0.2, seconds=0.3)
        self.assertGreater(mbps, 0)

    def test_slow_line_is_counted_as_it_arrives(self):
        # in 1 MiB reads, a stream on a slow line finished none in the window: nothing counted
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Trickle)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        mbps = sh.measure_download(f"http://127.0.0.1:{server.server_port}/big", streams=1,
                                   warmup=0.2, seconds=0.5)
        self.assertGreater(mbps, 0)
        self.assertLess(mbps, 20)

    def test_all_fail(self):
        with self.assertRaisesRegex(OSError, "speed test failed"):
            sh.speed_test([closed_port_url()], streams=2, warmup=0.2, seconds=0.3)


def _fields(*names):
    return [{"name": n, "value": None} for n in names]


TR_FIELDS = ("host", "port", "useSsl", "urlBase", "username", "password")
SCHEMAS = {  # field names as in the apps' TransmissionSettings.cs / SonarrSettings.cs
    "sonarr": {"downloadclient": [{"implementation": "Transmission", "fields": _fields(
        *TR_FIELDS, "tvCategory", "tvImportedCategory", "tvDirectory", "addPaused")}]},
    "radarr": {"downloadclient": [{"implementation": "Transmission", "fields": _fields(
        *TR_FIELDS, "movieCategory", "movieDirectory")}]},
    # v3, forked from Radarr: the same fields (connected with the ones its schema has)
    "whisparr": {"downloadclient": [{"implementation": "Transmission", "fields": _fields(
        *TR_FIELDS, "movieCategory", "movieImportedCategory", "movieDirectory")}]},
    "prowlarr": {"downloadclient": [{"implementation": "Transmission", "fields": _fields(
        *TR_FIELDS, "category", "directory", "priority")}],
        "applications": [{"implementation": impl, "syncLevel": "addOnly", "fields": _fields(
            "prowlarrUrl", "baseUrl", "apiKey", "syncCategories")}
            for impl in ("Sonarr", "Radarr", "Whisparr", "Lidarr")],
        "notification": [{"implementation": "Ntfy", "onHealthIssue": False, "fields": _fields(
            "serverUrl", "accessToken", "userName", "password", "priority", "topics", "tags",
            "clickUrl")}],
        # ncore.yml's settings plus Cardigann's own
        "indexer": [{"implementation": "Cardigann", "definitionName": "ncore", "name": "nCore",
                     "enable": False, "appProfileId": 0, "fields": _fields(
                         "definitionFile", "baseUrl", "username", "password", "2facode",
                         "torrentBaseSettings.seedRatio", "torrentBaseSettings.seedTime")},
                    {"implementation": "Cardigann", "definitionName": "other", "fields": []}]},
}


class FakeArr:
    """An *arr API in memory: GET/POST/PUT of download clients, applications, root folders.
    Passwords and API keys come back masked, like the real ones."""

    def __init__(self, name):
        self.name, self.base, self.items, self.calls = name, f"http://localhost/{name}", {}, []
        self.host = {"id": 1, "authenticationMethod": "none", "authenticationRequired": "enabled",
                     "username": "", "password": "", "allowedHosts": "", "port": 8989}
        self.items["qualityprofile"] = [
            {"id": 1, "name": "Any", "formatItems": [], "minFormatScore": 0},
            {"id": 2, "name": "HD-1080p", "formatItems": [], "minFormatScore": 0}]
        self.items["appprofile"] = [{"id": 1, "name": "Standard"}]
        self.commands = []

    def call(self, method, path, body=None):
        self.calls.append((method, path))
        if path == "config/host":
            return dict(self.host, password="********" if self.host["password"] else "")
        if path == "config/host/1":
            assert body["password"] == body["passwordConfirmation"]
            if body["authenticationRequired"] != "enabled" and not body["allowedHosts"]:
                raise OSError("400 Allowed Hosts is required")
            self.host = body
            return body
        kind, _, rest = path.partition("/")
        items = self.items.setdefault(kind, [])
        if method == "POST" and kind == "customformat":  # the app adds it to every profile
            body = dict(body, id=len(items) + 1)
            items.append(body)
            for profile in self.items["qualityprofile"]:
                profile["formatItems"].insert(0, {"format": body["id"], "name": body["name"],
                                                  "score": 0})
            return body
        if method == "GET" and rest == "schema":
            return json.loads(json.dumps(SCHEMAS[self.name][kind]))
        if method == "GET":
            out = json.loads(json.dumps(items))
            for item in out:
                for f in item.get("fields", []):
                    if f["name"] in ("password", "apiKey"):
                        f["value"] = sh.MASKED
            return out
        values = {f["name"]: f.get("value") for f in (body or {}).get("fields", [])}
        for cat, dir_ in (("tvCategory", "tvDirectory"), ("movieCategory", "movieDirectory")):
            if values.get(cat) and values.get(dir_):
                raise OSError("400 Cannot use Category and Directory")
        if kind == "command":
            self.commands.append(body)
            return body
        if method == "DELETE":
            items.pop([i["id"] for i in items].index(int(rest)))
            return None
        if method == "POST":
            items.append(dict(body, id=len(items) + 1))
            return body
        if method == "PUT":
            items[[i["id"] for i in items].index(int(rest))] = body
            return body
        raise AssertionError(method)

    def field(self, kind, name, field):
        item = next(i for i in self.items[kind] if i["name"] == name)
        return next(f["value"] for f in item["fields"] if f["name"] == field)


class ArrConnectTest(unittest.TestCase):
    def setUp(self):
        self.state = {}  # what the state file keeps between runs

    def connect(self, apis, password="pw", ncore=("bob", "npw", "123456")):
        return list(sh.arr_connect(
            apis, {"sonarr": "skey", "radarr": "rkey", "prowlarr": "pkey", "whisparr": "wkey"},
            "alice", password, "/mnt/storage/downloads", "/mnt/storage/MISC",
            ["/mnt/storage/SERIES/HD/HU", "/mnt/storage/SERIES/HD/EN"],
            ["/mnt/storage/MOVIES/HD/HU"], ncore, ("https://ntfy.sh", "torrent-box-x"),
            self.state, ["/mnt/storage/XXX/HD", "/mnt/storage/XXX/SD"]))

    def test_first_run_sets_everything_up(self):
        apis = {n: FakeArr(n) for n in ("sonarr", "radarr", "prowlarr")}
        out = self.connect(apis)
        self.assertIn("sonarr: download client Transmission: added", out)
        self.assertIn("prowlarr: application Radarr: added", out)
        s = apis["sonarr"]
        self.assertEqual(s.field("downloadclient", "Transmission", "tvDirectory"),
                         "/mnt/storage/downloads/series")
        self.assertEqual(s.field("downloadclient", "Transmission", "tvCategory"), "")  # not both
        self.assertEqual(s.field("downloadclient", "Transmission", "urlBase"), "/transmission/")
        self.assertEqual(s.field("downloadclient", "Transmission", "password"), "pw")
        self.assertIs(s.items["downloadclient"][0]["enable"], True)
        self.assertEqual([f["path"] for f in s.items["rootfolder"]],
                         ["/mnt/storage/SERIES/HD/HU", "/mnt/storage/SERIES/HD/EN"])
        r = apis["radarr"]
        self.assertEqual(r.field("downloadclient", "Transmission", "movieDirectory"),
                         "/mnt/storage/downloads/movies")
        self.assertEqual(r.field("downloadclient", "Transmission", "movieCategory"), "")
        p = apis["prowlarr"]
        self.assertEqual(p.field("downloadclient", "Transmission", "directory"), "/mnt/storage/MISC")
        self.assertEqual(p.field("applications", "Sonarr", "apiKey"), "skey")
        self.assertEqual(p.field("applications", "Radarr", "baseUrl"), "http://localhost/radarr")
        app = next(i for i in p.items["applications"] if i["name"] == "Sonarr")
        self.assertEqual((app["implementation"], app["syncLevel"]), ("Sonarr", "fullSync"))
        # imported downloads keep seeding
        self.assertIs(s.items["downloadclient"][0]["removeCompletedDownloads"], False)
        # every app gets the login page with the Transmission login
        for api in apis.values():
            self.assertEqual((api.host["authenticationMethod"], api.host["username"],
                              api.host["password"], api.host["authenticationRequired"]),
                             ("forms", "alice", "pw", "enabled"))
            self.assertEqual(api.host["port"], 8989)  # the rest is passed through
        # HUN preferred in every profile of Sonarr and Radarr
        for api in (s, r):
            fmt = api.items["customformat"][0]
            self.assertEqual(fmt["specifications"][0]["fields"][0]["value"], r"\bHUN\b")
            for profile in api.items["qualityprofile"]:
                self.assertEqual(profile["formatItems"],
                                 [{"format": fmt["id"], "name": "HUN", "score": sh.HUN_SCORE}])
        # nCore: added off (that login's session would be lost), then switched on with the
        # code by a PUT (logged in with an id: session kept), then sent to Sonarr and Radarr
        self.assertLess(p.calls.index(("POST", "indexer")), p.calls.index(("PUT", "indexer/1")))
        self.assertIn({"name": "ApplicationIndexerSync", "forceSync": True}, p.commands)
        # nCore with the 2FA code, in the default app profile, no seed limits
        ncore = p.items["indexer"][0]
        self.assertEqual((ncore["name"], ncore["enable"], ncore["appProfileId"]),
                         ("nCore", True, 1))
        self.assertEqual(p.field("indexer", "nCore", "2facode"), "123456")
        self.assertEqual(p.field("indexer", "nCore", "username"), "bob")
        self.assertIsNone(p.field("indexer", "nCore", "torrentBaseSettings.seedRatio"))
        # health alerts (e.g. the nCore login failing) go to the ntfy topic
        self.assertEqual(p.field("notification", "ntfy (torrent-box)", "topics"),
                         ["torrent-box-x"])
        self.assertIs(p.items["notification"][0]["onHealthIssue"], True)
        # grabs (Sonarr, Radarr, Prowlarr's search page) too, at low priority
        grabs = next(n for n in p.items["notification"] if n["name"] == "ntfy grabs (torrent-box)")
        self.assertEqual((grabs["onGrab"], grabs["includeManualGrabs"], grabs["onHealthIssue"]),
                         (True, True, False))
        self.assertEqual(p.field("notification", "ntfy grabs (torrent-box)", "priority"), 2)

    def test_existing_ncore_is_left_alone(self):
        apis = {n: FakeArr(n) for n in ("sonarr", "radarr", "prowlarr")}
        self.connect(apis)
        apis["prowlarr"].calls.clear()
        # a later run with a stale 2FA code: the indexer isn't re-saved (that would log in)
        out = self.connect(apis, ncore=("bob", "npw", "999999"))
        self.assertIn("prowlarr: nCore indexer: ok", out)
        self.assertEqual(apis["prowlarr"].field("indexer", "nCore", "2facode"), "123456")
        self.assertNotIn(("PUT", "indexer/1"), apis["prowlarr"].calls)
        # switched off by hand after it worked: stays off
        apis["prowlarr"].items["indexer"][0]["enable"] = False
        self.assertEqual(sh.ncore_status(apis["prowlarr"], self.state), "present")
        out = self.connect(apis, ncore=("bob", "npw", "999999"))
        self.assertIn("prowlarr: nCore indexer: ok", out)
        self.assertIs(apis["prowlarr"].items["indexer"][0]["enable"], False)

    def test_new_transmission_password_reaches_every_app(self):
        # the apps return it masked, so only the last run's fingerprint shows it changed
        apis = {n: FakeArr(n) for n in ("sonarr", "radarr", "prowlarr")}
        self.connect(apis)
        self.assertNotIn("pw", json.dumps(self.state))  # a salted hash, not the password
        out = self.connect(apis, password="new")
        for name in ("sonarr", "radarr", "prowlarr"):
            self.assertIn(f"{name}: login alice: set", out)
            self.assertIn(f"{name}: download client Transmission: updated", out)
            self.assertEqual(apis[name].field("downloadclient", "Transmission", "password"), "new")
            self.assertEqual(apis[name].host["password"], "new")
        self.assertIn("prowlarr: nCore indexer: ok", out)
        out = self.connect(apis, password="new")
        self.assertTrue(all(line.endswith(": ok") for line in out), out)

    def test_new_password_is_pushed_again_after_a_failed_run(self):
        apis = {n: FakeArr(n) for n in ("sonarr", "radarr", "prowlarr")}
        self.connect(apis)
        with mock.patch.object(apis["radarr"], "call", side_effect=OSError("radarr is down")):
            with self.assertRaises(OSError):
                self.connect(apis, password="new")
        self.assertEqual(apis["sonarr"].host["password"], "new")  # got it before the error
        out = self.connect(apis, password="new")
        self.assertIn("radarr: login alice: set", out)
        self.assertEqual(apis["radarr"].field("downloadclient", "Transmission", "password"), "new")

    def test_ncore_whose_first_login_failed_is_switched_on(self):
        apis = {n: FakeArr(n) for n in ("sonarr", "radarr", "prowlarr")}
        p = apis["prowlarr"]
        real_call = p.call

        def stale_code(method, path, body=None):
            if method == "PUT" and path.startswith("indexer/"):
                raise OSError("400 nCore: login failed (2FA code)")
            return real_call(method, path, body)

        with mock.patch.object(p, "call", side_effect=stale_code):
            with self.assertRaises(OSError):
                self.connect(apis, ncore=("bob", "npw", "000000"))
        self.assertIs(p.items["indexer"][0]["enable"], False)  # added, never switched on
        self.assertEqual(sh.ncore_status(p, self.state), "off")  # so setup asks for a code
        out = self.connect(apis, ncore=("bob", "npw", "654321"))
        self.assertIn("prowlarr: nCore indexer: switched on", out)
        self.assertIs(p.items["indexer"][0]["enable"], True)
        self.assertEqual(p.field("indexer", "nCore", "2facode"), "654321")
        self.assertEqual(len(p.items["indexer"]), 1)
        self.assertEqual(sh.ncore_status(p, self.state), "present")

    def test_ncore_status(self):
        p = FakeArr("prowlarr")
        self.assertEqual(sh.ncore_status(p, {}), "missing")
        p.items["indexer"] = [{"id": 1, "definitionName": "ncore", "enable": True, "fields": []}]
        self.assertEqual(sh.ncore_status(p, {}), "present")  # set up before the state file

    def test_new_2fa_code(self):
        apis = {n: FakeArr(n) for n in ("sonarr", "radarr", "prowlarr")}
        self.connect(apis)
        p = apis["prowlarr"]
        p.items["indexer"][0]["enable"] = False  # e.g. disabled by hand meanwhile
        sh.ncore_login(p, "654321")
        self.assertEqual(p.field("indexer", "nCore", "2facode"), "654321")
        self.assertIs(p.items["indexer"][0]["enable"], True)
        self.assertEqual(len(p.commands), 2)
        with self.assertRaisesRegex(ValueError, "no nCore"):
            sh.ncore_login(FakeArr("prowlarr"), "1")

    def test_radarr_profiles_take_any_language_once(self):
        apis = {n: FakeArr(n) for n in ("sonarr", "radarr", "prowlarr")}
        r = apis["radarr"]
        for profile in r.items["qualityprofile"]:  # Radarr's default; Sonarr's have none
            profile["language"] = {"id": -2, "name": "Original"}
        self.connect(apis)
        self.assertEqual([p["language"]["name"] for p in r.items["qualityprofile"]],
                         ["Any", "Any"])
        self.assertNotIn("language", apis["sonarr"].items["qualityprofile"][0])
        r.items["qualityprofile"][1]["language"] = {"id": 22, "name": "Hungarian"}  # yours
        self.connect(apis)
        self.assertEqual(r.items["qualityprofile"][1]["language"]["name"], "Hungarian")

    def test_hun_scores_you_changed_are_kept(self):
        apis = {n: FakeArr(n) for n in ("sonarr", "radarr", "prowlarr")}
        self.connect(apis)
        apis["sonarr"].items["qualityprofile"][0]["formatItems"][0]["score"] = 500
        apis["sonarr"].items["qualityprofile"][1]["formatItems"][0]["score"] = 0
        self.connect(apis)
        self.assertEqual(apis["sonarr"].items["qualityprofile"][0]["formatItems"][0]["score"], 500)
        self.assertEqual(apis["sonarr"].items["qualityprofile"][1]["formatItems"][0]["score"], 0)

    def test_hun_score_left_halfway_is_finished(self):
        s = FakeArr("sonarr")
        real_call = s.call

        def second_profile_fails(method, path, body=None):
            if method == "PUT" and path == "qualityprofile/2":
                raise OSError("500")
            return real_call(method, path, body)

        with mock.patch.object(s, "call", side_effect=second_profile_fails):
            with self.assertRaises(OSError):
                sh.ensure_hun_format(s, self.state)
        s.items["qualityprofile"][0]["formatItems"][0]["score"] = 30  # changed by you meanwhile
        self.assertEqual(sh.ensure_hun_format(s, self.state), "scored")
        self.assertEqual([p["formatItems"][0]["score"] for p in s.items["qualityprofile"]],
                         [30, sh.HUN_SCORE])
        self.assertEqual(len(s.items["customformat"]), 1)
        self.assertEqual(sh.ensure_hun_format(s, self.state), "ok")

    def test_state_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(sh.load_state(tmp), {})
            sh.save_state(tmp, {"ncore_on": True})
            path = Path(tmp) / sh.STATE_FILE
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(sh.load_state(tmp), {"ncore_on": True})
            path.write_text("not json")
            self.assertEqual(sh.load_state(tmp), {})

    def test_without_ncore_login_no_indexer(self):
        apis = {n: FakeArr(n) for n in ("sonarr", "radarr", "prowlarr")}
        out = self.connect(apis, ncore=None)
        self.assertFalse(any("nCore" in line for line in out))

    def test_rerun_changes_nothing(self):
        apis = {n: FakeArr(n) for n in ("sonarr", "radarr", "prowlarr")}
        self.connect(apis)
        for api in apis.values():
            api.calls.clear()
        out = self.connect(apis)
        self.assertTrue(all(line.endswith(": ok") for line in out), out)
        for api in apis.values():  # only reads, no writes
            self.assertEqual({m for m, _ in api.calls}, {"GET"}, api.name)

    def test_changed_setting_is_put_back_and_user_items_are_kept(self):
        apis = {n: FakeArr(n) for n in ("sonarr", "radarr", "prowlarr")}
        self.connect(apis)
        s = apis["sonarr"]
        client = s.items["downloadclient"][0]
        next(f for f in client["fields"] if f["name"] == "tvDirectory")["value"] = "/elsewhere"
        client["priority"] = 5  # a setting we don't own
        s.items["downloadclient"].append({"id": 2, "name": "qBittorrent", "fields": []})
        out = self.connect(apis)
        self.assertIn("sonarr: download client Transmission: updated", out)
        self.assertEqual(s.field("downloadclient", "Transmission", "tvDirectory"),
                         "/mnt/storage/downloads/series")
        self.assertEqual(s.items["downloadclient"][0]["priority"], 5)
        self.assertEqual(len(s.items["downloadclient"]), 2)

    def test_whisparr_is_connected_like_radarr(self):
        apis = {n: FakeArr(n) for n in ("sonarr", "radarr", "prowlarr", "whisparr")}
        out = self.connect(apis)
        self.assertIn("whisparr: login alice: set", out)
        self.assertIn("prowlarr: application Whisparr: added", out)
        w, p = apis["whisparr"], apis["prowlarr"]
        self.assertEqual(w.field("downloadclient", "Transmission", "movieDirectory"),
                         "/mnt/storage/downloads/xxx")
        self.assertEqual(w.field("downloadclient", "Transmission", "movieCategory"), "")
        self.assertIs(w.items["downloadclient"][0]["removeCompletedDownloads"], False)
        self.assertEqual([f["path"] for f in w.items["rootfolder"]],
                         ["/mnt/storage/XXX/HD", "/mnt/storage/XXX/SD"])
        self.assertEqual(p.field("applications", "Whisparr", "apiKey"), "wkey")
        self.assertEqual(p.field("applications", "Whisparr", "baseUrl"), "http://localhost/whisparr")
        for api in apis.values():
            api.calls.clear()
        out = self.connect(apis)  # a rerun only reads
        self.assertTrue(all(line.endswith(": ok") for line in out), out)
        for api in apis.values():
            self.assertEqual({m for m, _ in api.calls}, {"GET"}, api.name)

    def test_whisparr_switched_off_leaves_prowlarr(self):
        apis = {n: FakeArr(n) for n in ("sonarr", "radarr", "prowlarr", "whisparr")}
        self.connect(apis)
        del apis["whisparr"]
        out = self.connect(apis)
        self.assertIn("prowlarr: application Whisparr: removed", out)
        self.assertEqual([a["name"] for a in apis["prowlarr"].items["applications"]],
                         ["Sonarr", "Radarr"])
        self.assertNotIn("prowlarr: application Whisparr: removed", self.connect(apis))

    def test_whisparr_fields_from_its_schema(self):
        api = FakeArr("whisparr")
        self.assertEqual(sh.client_prefix(api), "movie")
        with mock.patch.dict(SCHEMAS["whisparr"], {"downloadclient": [
                {"implementation": "Transmission", "fields": _fields(*TR_FIELDS, "tvCategory",
                                                                     "tvDirectory")}]}):
            self.assertEqual(sh.client_prefix(api), "tv")  # v2, forked from Sonarr
        with mock.patch.dict(SCHEMAS["whisparr"], {"downloadclient": [
                {"implementation": "Transmission", "fields": _fields(*TR_FIELDS)}]}):
            with self.assertRaisesRegex(ValueError, "no Transmission category/directory"):
                sh.client_prefix(api)

    def test_unknown_field_is_an_error(self):
        api = FakeArr("radarr")
        with self.assertRaisesRegex(ValueError, "tvDirectory"):
            sh.ensure_item(api, "downloadclient", "Transmission", "Transmission",
                           {"tvDirectory": "/x"})

    def test_api_key_from_config_xml(self):
        with tempfile.TemporaryDirectory() as tmp:
            xml = Path(tmp) / "config.xml"
            self.assertIsNone(sh.arr_api_key(xml))  # not written yet
            xml.write_text("<Config><Port>8989</Port><ApiKey>abc123</ApiKey></Config>")
            self.assertEqual(sh.arr_api_key(xml), "abc123")
            xml.write_text("<Config><ApiKey>")  # half-written
            self.assertIsNone(sh.arr_api_key(xml))

    def test_compose_template(self):
        out = sh.render((REPO / "templates/arr-compose.yml").read_text(), {
            "PUID": "1000", "PGID": "107", "TZ": "Europe/Budapest",
            "ARR_DIR": "/home/me/.local/share/torrent-box/arr", "STORAGE_ROOT": "/mnt/storage"})
        self.assertIn("- /mnt/storage:/mnt/storage", out)  # same path as Transmission's
        self.assertIn('user: "1000:107"', out)
        self.assertIn("- /home/me/.local/share/torrent-box/arr/sonarr:/config", out)
        self.assertEqual(out.count("network_mode: host"), 2)  # the anchor + seerr
        for app in ("prowlarr", "sonarr", "radarr", "seerr"):
            self.assertIn(f"container_name: {app}\n", out)
        self.assertNotIn("container_name: whisparr", out)

    def test_compose_template_with_whisparr(self):
        # setup.sh appends it: it has to go on as one more service under services:
        text = (REPO / "templates/arr-compose.yml").read_text() + \
            (REPO / "templates/arr-compose-whisparr.yml").read_text()
        out = sh.render(text, {"PUID": "1000", "PGID": "107", "TZ": "UTC",
                               "ARR_DIR": "/arr", "STORAGE_ROOT": "/mnt/storage"})
        services = out.split("\nservices:\n", 1)[1]
        self.assertRegex(services, r"\n  whisparr:\n    <<: \*arr\n    image: ")
        self.assertIn("- /arr/whisparr:/config", services)
        self.assertEqual(services.count("- /mnt/storage:/mnt/storage"), 3)
        try:
            import yaml
        except ImportError:
            return
        doc = yaml.safe_load(out)
        self.assertEqual(list(doc["services"]),
                         ["prowlarr", "sonarr", "radarr", "seerr", "whisparr"])
        self.assertEqual(doc["services"]["whisparr"]["network_mode"], "host")


class FakeJellyfin:
    """Jellyfin's wizard and library endpoints, as http_json sees them."""

    def __init__(self, completed=False):
        self.completed, self.user_init, self.user, self.libraries, self.calls = \
            completed, False, None, [], []

    def __call__(self, method, url, body=None, headers=None, opener=None, timeout=30):
        path = urllib.parse.urlsplit(url).path
        self.calls.append((method, path))
        if path == "/System/Info/Public":
            return {"StartupWizardCompleted": self.completed}
        assert not self.completed, f"{method} {path} after the wizard"
        if path == "/Startup/User" and method == "GET":
            self.user_init = True
            return {"Name": "root"}
        if path == "/Startup/User":
            if not self.user_init:  # the GET creates the first user
                raise OSError("404")
            self.user = body
            return None
        if path == "/Library/VirtualFolders" and method == "GET":
            return [{"Name": n} for n, _, _ in self.libraries]
        if path == "/Library/VirtualFolders":
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            self.libraries.append((q["name"][0], q["collectionType"][0],
                                   (q["paths"][0].split(","), body["LibraryOptions"])))
            return None
        if path == "/Startup/Complete":
            self.completed = True
        return None


class JellyfinSetupTest(unittest.TestCase):
    def config(self, tmp):
        cfg = Path(tmp) / "config.ini"
        cfg.write_text((REPO / "config.example.ini").read_text())
        return cfg

    def test_libraries_from_routes(self):
        with tempfile.TemporaryDirectory() as tmp:
            libs = sh.jellyfin_libraries(self.config(tmp))
        self.assertEqual([(n, t) for n, t, *_ in libs], [
            ("Filmek", "movies"), ("Movies", "movies"), ("Sorozatok", "tvshows"),
            ("Series", "tvshows"), ("Music", "music")])
        self.assertEqual(libs[0][2], ["/mnt/storage/MOVIES/HD/HU", "/mnt/storage/MOVIES/SD/HU"])
        self.assertEqual(libs[0][3:], ("hu", "HU"))
        self.assertEqual(libs[4][2], ["/mnt/storage/MUSIC"])  # the top folder, once

    def test_wizard(self):
        fake = FakeJellyfin()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(sh, "http_json", fake):
            out = list(sh.jellyfin_setup(self.config(tmp), "alice", "pw"))
        self.assertEqual(fake.user, {"Name": "alice", "Password": "pw"})
        self.assertTrue(fake.completed)
        self.assertEqual(fake.calls[-1], ("POST", "/Startup/Complete"))
        names = [n for n, _, _ in fake.libraries]
        self.assertEqual(names, ["Filmek", "Movies", "Sorozatok", "Series", "Music"])
        self.assertEqual(fake.libraries[2][2][1]["PreferredMetadataLanguage"], "hu")
        self.assertIn("first-run wizard completed", out)

    def test_set_up_jellyfin_is_left_alone(self):
        fake = FakeJellyfin(completed=True)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(sh, "http_json", fake):
            out = list(sh.jellyfin_setup(self.config(tmp), "alice", "pw"))
        self.assertEqual(fake.calls, [("GET", "/System/Info/Public")])
        self.assertIn("already set up", out[0])


class JellyfinKeyTest(unittest.TestCase):
    def fake(self, keys, admin=True):
        calls = []

        def http_json(method, url, body=None, headers=None, opener=None, timeout=30):
            calls.append((method, url))
            if url.endswith("/Users/AuthenticateByName"):
                return {"AccessToken": "T", "User": {"Policy": {"IsAdministrator": admin}}}
            if url.endswith("/Auth/Keys") and method == "GET":
                return {"Items": [{"AppName": a, "AccessToken": t} for a, t in keys.items()]}
            if "/Auth/Keys?" in url and method == "POST":
                keys[url.split("app=")[1]] = "NEW"
                return None
            raise AssertionError(url)
        return calls, http_json

    def test_existing_key_is_reused(self):
        calls, fake = self.fake({"Seerr": "S", "Desireerr": "D"})
        with mock.patch.object(sh, "http_json", fake):
            self.assertEqual(sh.jellyfin_api_key("u", "p", "Desireerr"), "D")
        self.assertNotIn("POST", [m for m, u in calls if "Keys" in u])

    def test_made_when_missing(self):
        calls, fake = self.fake({"Seerr": "S"})
        with mock.patch.object(sh, "http_json", fake):
            self.assertEqual(sh.jellyfin_api_key("u", "p", "Desireerr"), "NEW")
        self.assertIn(("POST", "http://localhost:8096/Auth/Keys?app=Desireerr"), calls)

    def test_needs_an_admin(self):
        _, fake = self.fake({}, admin=False)
        with mock.patch.object(sh, "http_json", fake), self.assertRaises(OSError):
            sh.jellyfin_api_key("u", "p", "Desireerr")


class JellyfinXxxLibraryTest(unittest.TestCase):
    ROOTS = ["/mnt/storage/XXX/HD", "/mnt/storage/XXX/SD/"]

    def run_with(self, folders, admin=True):
        calls = []

        def http_json(method, url, body=None, headers=None, opener=None, timeout=30):
            calls.append((method, url, body))
            if url.endswith("/Users/AuthenticateByName"):
                return {"AccessToken": "T", "User": {"Policy": {"IsAdministrator": admin}}}
            if url.endswith("/Library/VirtualFolders") and method == "GET":
                return folders
            if method == "POST":
                return None
            raise AssertionError(url)
        with mock.patch.object(sh, "http_json", http_json):
            out = list(sh.jellyfin_xxx_library("u", "p", self.ROOTS))
        return out, [(m, u, b) for m, u, b in calls if m == "POST" and "Authenticate" not in u]

    def test_made_as_home_videos(self):
        out, posts = self.run_with([{"Name": "Movies", "CollectionType": "movies", "Locations": ["/m"]}])
        (method, url, body), = posts
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.assertEqual((q["name"], q["collectionType"]), (["XXX"], ["homevideos"]))
        self.assertEqual(q["paths"], ["/mnt/storage/XXX/HD,/mnt/storage/XXX/SD"])
        self.assertEqual(body["LibraryOptions"], {"EnableRealtimeMonitor": True, "EnablePhotos": False})
        self.assertIn("Home videos and photos", out[0])

    def test_name_taken_by_another(self):
        _, posts = self.run_with([{"Name": "XXX", "CollectionType": "homevideos", "Locations": ["/x"]}])
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(posts[0][1]).query)
        self.assertEqual(q["name"], ["XXX (Whisparr)"])

    def test_existing_library_kept_and_completed(self):
        out, posts = self.run_with([{"Name": "Adult", "CollectionType": "homevideos",
                                     "Locations": ["/mnt/storage/XXX/HD"]}])
        self.assertEqual(posts, [("POST", "http://localhost:8096/Library/VirtualFolders/Paths?refreshLibrary=true",
                                  {"Name": "Adult", "PathInfo": {"Path": "/mnt/storage/XXX/SD"}})])
        self.assertTrue(out[-1].startswith("library Adult (Home videos"))

    def test_movies_library_gets_a_warning(self):
        out, posts = self.run_with([{"Name": "XXX", "CollectionType": "movies",
                                     "Locations": ["/mnt/storage/XXX/HD", "/mnt/storage/XXX/SD"]}])
        self.assertEqual(posts, [])
        self.assertTrue(out[-1].startswith("warning: library XXX has Whisparr's folders as movies"))

    def test_needs_an_admin(self):
        with self.assertRaises(OSError):
            self.run_with([], admin=False)


class FakeSeerr:
    def __init__(self, initialized=False):
        self.public = {"initialized": initialized, "mediaServerType": 2 if initialized else 4}
        self.arrs = {"sonarr": [], "radarr": []}
        self.libraries = [{"id": "a", "name": "Filmek", "enabled": False},
                          {"id": "b", "name": "Sorozatok", "enabled": True}]
        self.calls, self.signin = [], None
        self.ntfy = {"enabled": False, "embedPoster": True, "types": 0,
                     "options": {"url": "", "topic": "", "tags": "", "priority": 3}}
        self.main = {"apiKey": "seerrkey", "applicationUrl": "", "applicationTitle": "Seerr"}

    def __call__(self, method, url, body=None, headers=None, opener=None, timeout=30):
        path = urllib.parse.urlsplit(url).path.removeprefix("/api/v1/")
        self.calls.append((method, path, (headers or {}).get("X-Api-Key")))
        if path == "settings/public":
            return self.public
        if path == "auth/jellyfin":
            if self.public["mediaServerType"] == 2 and "hostname" in body:
                raise OSError("500 Jellyfin hostname already configured")
            self.signin, self.public["mediaServerType"] = body, 2
            return {"id": 1}
        if path == "settings/jellyfin/library/sync":
            return self.libraries
        if path.startswith("settings/jellyfin/library/"):
            next(l for l in self.libraries if l["id"] == path.rsplit("/", 1)[1])["enabled"] = \
                body["enabled"]
            return None
        kind = path.split("/")[1] if path.startswith("settings/") else ""
        if kind in self.arrs and path.endswith("/test"):
            return {"profiles": [{"id": 1, "name": "Any"}, {"id": 4, "name": "HD-1080p"}],
                    "rootFolders": []}
        if kind in self.arrs and method == "GET":
            return self.arrs[kind]
        if kind in self.arrs:
            self.arrs[kind].append(body)  # no de-duplication, like the real one
            return body
        if path == "settings/notifications/ntfy":
            if method == "POST":
                self.ntfy = body
            return self.ntfy
        if path == "settings/main":
            if method == "POST":
                if "apiKey" in body:
                    raise OSError("400 request/body/apiKey is read-only")
                self.main = dict(body, apiKey=self.main["apiKey"])
            return self.main
        if path == "settings/initialize":
            self.public["initialized"] = True
            return self.public
        raise AssertionError(path)


class SeerrSetupTest(unittest.TestCase):
    def run_setup(self, fake, settings_json="/nonexistent/settings.json",
                  ntfy=("https://ntfy.sh", "topic1")):
        with mock.patch.object(sh, "http_json", fake):
            return list(sh.seerr_setup(settings_json, "alice", "pw",
                                       {"sonarr": "skey", "radarr": "rkey"},
                                       "/mnt/storage/SERIES/HD/HU", "/mnt/storage/MOVIES/HD/HU",
                                       ntfy, "http://box:5055"))

    def test_ntfy_and_app_url(self):
        fake = FakeSeerr()
        out = self.run_setup(fake)
        self.assertIn("requests and issues to ntfy: added", out)
        self.assertIn("application URL (http://box:5055): set", out)
        self.assertEqual((fake.ntfy["enabled"], fake.ntfy["types"]), (True, 2 | 16 | 128 | 256))
        self.assertEqual(fake.ntfy["options"], {"url": "https://ntfy.sh", "topic": "topic1",
                                                "tags": "", "priority": 3})
        self.assertEqual((fake.main["applicationUrl"], fake.main["applicationTitle"]),
                         ("http://box:5055", "Seerr"))
        # your own choices in Seerr stay: types, an application URL, switched off
        fake.ntfy["types"] = 2
        fake.main["applicationUrl"] = "https://seerr.example"
        out = self.run_setup(fake, ntfy=("https://ntfy.sh", "topic2"))
        self.assertIn("requests and issues to ntfy: updated", out)
        self.assertIn("application URL (http://box:5055): ok", out)
        self.assertEqual((fake.ntfy["types"], fake.ntfy["options"]["topic"]), (2, "topic2"))
        self.assertIn("requests and issues to ntfy: ok", self.run_setup(fake, ntfy=("https://ntfy.sh", "topic2")))
        fake.ntfy["enabled"] = False
        self.assertIn("requests and issues to ntfy: off", self.run_setup(fake, ntfy=("https://ntfy.sh", "topic3")))
        self.assertEqual(fake.ntfy["options"]["topic"], "topic2")

    def test_first_run(self):
        fake = FakeSeerr()
        out = self.run_setup(fake)
        self.assertEqual((fake.signin["serverType"], fake.signin["hostname"],
                          fake.signin["port"]), (2, "localhost", 8096))
        self.assertTrue(all(l["enabled"] for l in fake.libraries))
        sonarr = fake.arrs["sonarr"][0]
        self.assertEqual((sonarr["activeProfileName"], sonarr["activeDirectory"], sonarr["port"],
                          sonarr["isDefault"], sonarr["apiKey"]),
                         ("HD-1080p", "/mnt/storage/SERIES/HD/HU", 8989, True, "skey"))
        self.assertEqual(fake.arrs["radarr"][0]["minimumAvailability"], "released")
        self.assertTrue(fake.public["initialized"])
        self.assertEqual(out[-1], "setup finished")

    def test_later_run_uses_the_api_key_and_adds_nothing_twice(self):
        fake = FakeSeerr()
        self.run_setup(fake)
        fake.calls.clear()
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / "settings.json"
            settings.write_text(json.dumps({"main": {"apiKey": "seerrkey"}}))
            out = self.run_setup(fake, settings)
        self.assertEqual(out, ["sonarr: ok", "radarr: ok", "application URL (http://box:5055): ok",
                               "requests and issues to ntfy: ok"])
        self.assertEqual(len(fake.arrs["sonarr"]), 1)
        self.assertNotIn("auth/jellyfin", [p for _, p, _ in fake.calls])
        self.assertTrue(all(k == "seerrkey" for _, p, k in fake.calls if p != "settings/public"))


class RpcCheckTest(unittest.TestCase):
    def test_login(self):
        with FakeServer() as server:
            url = f"{server.base_url}/transmission/rpc"
            self.assertEqual(sh.rpc_check(url, "user", "pass", 5), (0, "login works"))
            self.assertEqual(sh.rpc_check(url, "user", "wrong", 5)[0], 3)

    def test_unreachable(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self.assertEqual(sh.rpc_check(f"http://127.0.0.1:{port}/transmission/rpc", "u", "p", 5)[0], 4)


def run_main(argv, stdin=""):
    out = io.StringIO()
    with mock.patch("sys.stdin", io.StringIO(stdin)), contextlib.redirect_stdout(out), \
            contextlib.redirect_stderr(io.StringIO()):
        code = sh.main(argv)
    return code, out.getvalue()


if __name__ == "__main__":
    unittest.main()


VAINFO_780M = """vainfo: Driver version: Mesa Gallium driver for AMD Radeon 780M Graphics
      VAProfileH264Main               :\tVAEntrypointVLD
      VAProfileH264Main               :\tVAEntrypointEncSlice
      VAProfileH264High               :\tVAEntrypointVLD
      VAProfileHEVCMain               :\tVAEntrypointVLD
      VAProfileHEVCMain10             :\tVAEntrypointVLD
      VAProfileJPEGBaseline           :\tVAEntrypointVLD
      VAProfileVP9Profile0            :\tVAEntrypointVLD
      VAProfileVP9Profile2            :\tVAEntrypointVLD
      VAProfileAV1Profile0            :\tVAEntrypointVLD
      VAProfileNone                   :\tVAEntrypointVideoProc
"""


class HwaccelTest(unittest.TestCase):
    def test_vaapi_caps(self):
        self.assertEqual(sh.vaapi_caps(VAINFO_780M), (["h264", "hevc", "vp9", "av1"], True, True, True))
        self.assertEqual(sh.vaapi_caps("VAProfileMPEG2Main : VAEntrypointVLD"),
                         (["mpeg2video"], False, False, False))
        self.assertEqual(sh.vaapi_caps("libva error: no driver"), ([], False, False, False))

    def fake_jellyfin(self, options, admin=True):
        calls = []

        def http_json(method, url, body=None, headers=None, opener=None, timeout=30):
            path = urllib.parse.urlsplit(url).path
            calls.append((method, path, body))
            if path == "/Users/AuthenticateByName":
                return {"AccessToken": "tok", "User": {"Policy": {"IsAdministrator": admin}}}
            if path == "/System/Configuration/encoding":
                self.assertIn('Token="tok"', headers["Authorization"])
                if method == "POST":
                    options.clear()
                    options.update(body)
                return dict(options)
            raise AssertionError(path)
        return calls, http_json

    def test_switched_on_once(self):
        options = {"HardwareAccelerationType": "none", "VaapiDevice": "", "EnableTonemapping": False,
                   "HardwareDecodingCodecs": ["h264", "vc1"], "EnableHardwareEncoding": True}
        calls, fake = self.fake_jellyfin(options)
        with mock.patch.object(sh, "http_json", fake):
            line = sh.jellyfin_hwaccel("me", "pw", "/dev/dri/renderD128", VAINFO_780M)
            self.assertIn("decodes h264, hevc, vp9, av1, encodes H.264", line)
            self.assertEqual((options["HardwareAccelerationType"], options["VaapiDevice"],
                              options["HardwareDecodingCodecs"], options["EnableDecodingColorDepth10Hevc"]),
                             ("vaapi", "/dev/dri/renderD128", ["h264", "hevc", "vp9", "av1"], True))
            self.assertFalse(options["EnableTonemapping"])  # the rest is kept
            # already on (or something you chose): left alone
            calls.clear()
            self.assertIn("already set (vaapi)", sh.jellyfin_hwaccel("me", "pw", "/dev/dri/renderD128", VAINFO_780M))
            self.assertNotIn("POST", [m for m, p, _ in calls if p.startswith("/System")])

    def test_not_admin_or_no_h264(self):
        _, fake = self.fake_jellyfin({"HardwareAccelerationType": "none"}, admin=False)
        with mock.patch.object(sh, "http_json", fake):
            with self.assertRaisesRegex(OSError, "not a Jellyfin admin"):
                sh.jellyfin_hwaccel("me", "pw", "/dev/dri/renderD128", VAINFO_780M)
            self.assertIn("left on the CPU", sh.jellyfin_hwaccel("me", "pw", "/dev/x", "nothing"))


class SmartdConfTest(unittest.TestCase):
    DEBIAN = "# smartd.conf\n# DEVICESCAN -a\nDEVICESCAN -d removable -n standby -m root -M exec /usr/share/smartmontools/smartd-runner\n"

    def test_inserted_before_devicescan_and_replaced(self):
        out = sh.smartd_conf(self.DEBIAN, "/dev/disk/by-id/ata-X -a")
        lines = out.splitlines()
        i = lines.index("/dev/disk/by-id/ata-X -a")
        self.assertEqual(lines[i - 1], sh.SMARTD_BEGIN)
        self.assertEqual(lines[i + 1], sh.SMARTD_END)
        self.assertTrue(lines[i + 3].startswith("DEVICESCAN"))
        self.assertIn("# DEVICESCAN -a", lines[:i])  # a commented one doesn't count
        again = sh.smartd_conf(out, "/dev/disk/by-id/ata-Y -a")
        self.assertNotIn("ata-X", again)
        self.assertEqual(again.count(sh.SMARTD_BEGIN), 1)
        self.assertEqual(sh.smartd_conf(again, "/dev/disk/by-id/ata-Y -a"), again)

    def test_no_devicescan_or_empty(self):
        self.assertTrue(sh.smartd_conf("", "/dev/sda -a").startswith(sh.SMARTD_BEGIN))
        self.assertTrue(sh.smartd_conf("/dev/sdb -a\n", "/dev/sda -a").startswith("/dev/sdb -a\n" + sh.SMARTD_BEGIN))


class ArrUpdateTest(unittest.TestCase):
    """templates/arr-update with a fake docker: which apps got a new image."""

    def run_update(self, pull_fails=False):
        tmp = Path(self.tmpdir.name)
        (tmp / "pushes").write_text("")
        result = subprocess.run(["bash", str(REPO / "templates/arr-update"), "compose.yml"],
                                capture_output=True, text=True,
                                env={**os.environ, "PATH": f"{tmp}:{os.environ['PATH']}",
                                     "TORRENT_BOX_PUSH": str(tmp / "push"),
                                     "PULL_FAILS": "1" if pull_fails else ""})
        return result, (tmp / "pushes").read_text()

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        tmp = Path(self.tmpdir.name)
        (tmp / "images").write_text("sonarr=sha256:a\nradarr=sha256:b\n")
        # fake docker: "compose up" applies the "pulled" images
        (tmp / "docker").write_text(f"""#!/bin/bash
case "$*" in
  *"config --services") printf 'sonarr\\nradarr\\n' ;;
  *"pull --quiet") [[ -z $PULL_FAILS ]] ;;
  *"up -d"*) [[ ! -f {tmp}/pulled ]] || cp {tmp}/pulled {tmp}/images ;;
  inspect*) grep "^${{@: -1}}=" {tmp}/images | cut -d= -f2 ;;
  "image prune -f") ;;
  *) echo "unexpected: $*" >&2; exit 9 ;;
esac
""")
        (tmp / "push").write_text(f'#!/bin/sh\nprintf "%s|%s\\n" "$1" "$2" >>{tmp}/pushes\n')
        for f in ("docker", "push"):
            (tmp / f).chmod(0o755)

    def test_nothing_new(self):
        result, pushes = self.run_update()
        self.assertEqual((result.returncode, result.stdout.strip(), pushes), (0, "all up to date", ""))

    def test_updated_apps_are_pushed(self):
        (Path(self.tmpdir.name) / "pulled").write_text("sonarr=sha256:a\nradarr=sha256:NEW\n")
        result, pushes = self.run_update()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(pushes, "Apps updated|New versions running: radarr\n")

    def test_failure_is_pushed(self):
        result, pushes = self.run_update(pull_fails=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("App update failed|couldn't download the new images", pushes)


GIB = 1 << 30
# lsblk -J -b, newer util-linux: partitions listed flat with PKNAME, booleans as true/false
LSBLK_FLAT = json.dumps({"blockdevices": [
    {"path": "/dev/loop0", "pkname": None, "type": "loop", "size": 70 << 20, "fstype": "squashfs",
     "pttype": None, "mountpoint": "/snap/core24/1643", "ro": True, "uuid": None, "label": None,
     "model": None},
    {"path": "/dev/nvme0n1", "pkname": None, "type": "disk", "size": 954 * GIB, "fstype": None,
     "pttype": "gpt", "mountpoint": None, "ro": False, "uuid": None, "label": None, "model": "Vi3000"},
    {"path": "/dev/nvme0n1p1", "pkname": "nvme0n1", "type": "part", "size": GIB, "fstype": "vfat",
     "pttype": "gpt", "mountpoint": "/boot/efi", "ro": False, "uuid": "A1B2-C3D4", "label": None,
     "model": None},
    {"path": "/dev/nvme0n1p2", "pkname": "nvme0n1", "type": "part", "size": 952 * GIB,
     "fstype": "ext4", "pttype": "gpt", "mountpoint": "/", "ro": False, "uuid": "root-uuid",
     "label": None, "model": None},
    {"path": "/dev/sda", "pkname": None, "type": "disk", "size": 11176 * GIB, "fstype": None,
     "pttype": None, "mountpoint": None, "ro": False, "uuid": None, "label": None,
     "model": "ST12000VN0008"},
    {"path": "/dev/sdb", "pkname": None, "type": "disk", "size": 3726 * GIB, "fstype": None,
     "pttype": "gpt", "mountpoint": None, "ro": False, "uuid": None, "label": None,
     "model": "WD40EFRX"},
    {"path": "/dev/sdb1", "pkname": "sdb", "type": "part", "size": 3726 * GIB, "fstype": "ntfs",
     "pttype": "gpt", "mountpoint": None, "ro": False, "uuid": "ntfs-uuid", "label": "Media",
     "model": None},
    {"path": "/dev/sdc", "pkname": None, "type": "disk", "size": 1863 * GIB, "fstype": None,
     "pttype": "gpt", "mountpoint": None, "ro": False, "uuid": None, "label": None, "model": "old"},
    {"path": "/dev/sdd", "pkname": None, "type": "disk", "size": 1863 * GIB,
     "fstype": "linux_raid_member", "pttype": None, "mountpoint": None, "ro": False,
     "uuid": "raid-uuid", "label": None, "model": "raid"},
    {"path": "/dev/sde", "pkname": None, "type": "disk", "size": 512 << 20, "fstype": None,
     "pttype": None, "mountpoint": None, "ro": False, "uuid": None, "label": None, "model": "tiny"},
    {"path": "/dev/sdf", "pkname": None, "type": "disk", "size": 64 * GIB, "fstype": None,
     "pttype": None, "mountpoint": None, "ro": True, "uuid": None, "label": None, "model": "ro"},
    {"path": "/dev/zram0", "pkname": None, "type": "disk", "size": 8 * GIB, "fstype": None,
     "pttype": None, "mountpoint": None, "ro": False, "uuid": None, "label": None, "model": None},
]})


class StorageTest(unittest.TestCase):
    def test_candidates(self):
        got = sh.storage_candidates(LSBLK_FLAT)
        self.assertEqual([(path, kind, uuid) for path, kind, uuid, _ in got], [
            ("/dev/sda", "blank", ""),           # biggest first
            ("/dev/sdb1", "fs", "ntfs-uuid"),    # an unmounted filesystem, kept as it is
        ])
        self.assertEqual(got[0][3], "10.9T blank ST12000VN0008: partitioned and formatted ext4")
        self.assertEqual(got[1][3], "3.6T ntfs Media WD40EFRX")  # the disk's model, from PKNAME

    def test_never_offered(self):
        paths = [c[0] for c in sh.storage_candidates(LSBLK_FLAT)]
        for path in ("/dev/nvme0n1p2",  # mounted
                     "/dev/sdb",        # has a partition
                     "/dev/sdc",        # a partition table, even with no partitions
                     "/dev/sdd",        # part of a RAID
                     "/dev/sde",        # under 1 GiB
                     "/dev/sdf",        # read-only
                     "/dev/zram0", "/dev/loop0"):
            self.assertNotIn(path, paths)

    def test_nested_and_old_lsblk(self):
        # older lsblk: partitions nested under "children", booleans and sizes as strings
        tree = {"blockdevices": [
            {"path": "/dev/sdb", "type": "disk", "size": str(3726 * GIB), "fstype": None,
             "pttype": None, "mountpoint": None, "ro": "0", "uuid": None, "label": None,
             "model": "WD40EFRX", "children": [
                 {"path": "/dev/sdb1", "type": "part", "size": str(3726 * GIB), "fstype": "ext4",
                  "pttype": None, "mountpoint": None, "ro": "0", "uuid": "u1", "label": None,
                  "model": None}]},
            {"path": "/dev/sdg", "type": "disk", "size": str(64 * GIB), "fstype": None,
             "pttype": None, "mountpoint": None, "ro": "1", "uuid": None, "label": None,
             "model": None}]}
        self.assertEqual(sh.storage_candidates(json.dumps(tree)),
                         [("/dev/sdb1", "fs", "u1", "3.6T ext4 WD40EFRX")])  # sdb isn't blank

    def test_cli(self):
        code, out = run_main(["storage-candidates"], stdin=LSBLK_FLAT)
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[0].split("\t")[:3], ["/dev/sda", "blank", ""])

class WhisparrStashdbKey(unittest.TestCase):
    """The key of Whisparr's StashDB import list, for Desireerr."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.arr = Path(self.tmp.name)
        (self.arr / "whisparr").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def make_db(self, rows):
        import sqlite3
        db = sqlite3.connect(self.arr / "whisparr" / "whisparr3.db")
        db.execute('CREATE TABLE "ImportLists" ("Id" INTEGER PRIMARY KEY, "Enabled" INTEGER, '
                   '"Implementation" TEXT, "Settings" TEXT)')
        db.executemany("INSERT INTO ImportLists (Enabled, Implementation, Settings) VALUES (?,?,?)", rows)
        db.commit()
        db.close()

    def test_key_of_the_stashdb_list(self):
        self.make_db([(1, "TMDbPopularImport", '{"apiKey": "nope"}'),
                      (0, "StashDBFavoriteImport", '{"apiKey": ""}'),
                      (1, "StashDBFavoriteImport", '{"apiKey": " KEY1 ", "limit": 100}')])
        self.assertEqual(sh.whisparr_stashdb_key(self.arr), "KEY1")
        self.assertEqual(run_main(["whisparr-stashdb-key", "--arr-dir", str(self.arr)]), (0, "KEY1\n"))

    def test_none(self):
        self.assertEqual(sh.whisparr_stashdb_key(self.arr), "")
        self.make_db([(1, "StashDBFavoriteImport", "not json")])
        self.assertEqual(sh.whisparr_stashdb_key(self.arr), "")
