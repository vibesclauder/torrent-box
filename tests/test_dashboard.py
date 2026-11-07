import http.client
import json
import re
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from helpers import FakeServer, write_config
import dashboard as db

RUNNING = lambda unit: (db.OK, "running")  # noqa: E731


def torrent(name, status, error=0, down=0, up=0, error_string=""):
    return {"name": name, "status": status, "error": error, "errorString": error_string,
            "rateDownload": down, "rateUpload": up}


class FakeArr:
    """An *arr app's /api/vN/health and /ping."""

    def __init__(self, issues):
        self.issues = issues
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path == "/ping" or self.headers.get("X-Api-Key") == "KEY":
                    body = b"{}" if self.path == "/ping" else json.dumps(outer.issues).encode()
                    self.send_response(200)
                else:
                    body = b""
                    self.send_response(401)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, args=(0.01,), daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class FakeProwlarr:
    """Prowlarr's indexer API: the nCore indexer logs in when saved with GOOD_CODE."""

    GOOD_CODE = "123456"

    def __init__(self):
        self.indexer = {"id": 7, "name": "nCore", "definitionName": "ncore", "enable": True,
                        "fields": [{"name": "username", "value": "me"},
                                   {"name": "password", "value": "********"},
                                   {"name": "2facode", "value": "000000"}]}
        self.status = []
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/api/v1/indexer":
                    return self.reply(200, [{"id": 1, "definitionName": "other"}, outer.indexer])
                if self.path == "/api/v1/indexerstatus":
                    return self.reply(200, outer.status)
                self.reply(404, {})

            def do_PUT(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(("PUT", self.path, body))
                code = {f["name"]: f["value"] for f in body["fields"]}["2facode"]
                if code != outer.GOOD_CODE:
                    return self.reply(400, [{"propertyName": "", "errorMessage": "Login failed"}])
                outer.indexer = body
                self.reply(202, body)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(("POST", self.path, body))
                self.reply(201, {})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, args=(0.01,), daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class NcoreTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.arr_dir = Path(self.tmpdir.name)
        (self.arr_dir / "prowlarr").mkdir()
        (self.arr_dir / "prowlarr/config.xml").write_text("<Config><ApiKey>KEY</ApiKey></Config>")
        self.prowlarr = FakeProwlarr()
        patch = mock.patch.dict(db.ARR_APPS, {"prowlarr": (self.prowlarr.port, "v1")})
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self.prowlarr.close)
        self.addCleanup(self.tmpdir.cleanup)

    def test_status(self):
        now = 1_800_000_000
        c = db.check_ncore(self.arr_dir, None, now)
        self.assertEqual((c["state"], c["summary"], c["needs_code"]), (db.OK, "logged in", False))
        self.prowlarr.indexer["enable"] = False
        c = db.check_ncore(self.arr_dir, None, now)
        self.assertEqual((c["state"], c["needs_code"]), (db.WARN, True))
        self.prowlarr.indexer["enable"] = True
        failed = time.strftime("%Y-%m-%dT%H:%M:%S.1234567Z", time.gmtime(now - 7200))
        until = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 600))
        self.prowlarr.status = [{"indexerId": 7, "mostRecentFailure": failed, "disabledTill": until}]
        c = db.check_ncore(self.arr_dir, None, now)
        self.assertEqual((c["state"], c["needs_code"]), (db.WARN, True))
        self.assertIn("failing since 2 h ago", c["summary"])
        # logged in with a new code since: fine, though Prowlarr's backoff hasn't run out
        c = db.check_ncore(self.arr_dir, {"time": now - 60, "ok": True, "message": ""}, now)
        self.assertEqual(c["state"], db.OK)
        self.assertEqual(len(c["details"]), 1)
        self.assertIn("logged in with the new code", c["details"][0])
        # no Prowlarr
        self.prowlarr.close()
        self.assertEqual(db.check_ncore(self.arr_dir, None, now)["state"], db.DOWN)
        self.prowlarr.close = lambda: None

    def test_login(self):
        db.ncore_login(self.arr_dir, FakeProwlarr.GOOD_CODE)
        (method, path, body), (_, cmd_path, cmd) = self.prowlarr.requests
        self.assertEqual((method, path), ("PUT", "/api/v1/indexer/7"))
        self.assertTrue(body["enable"])
        self.assertIn({"name": "password", "value": "********"}, body["fields"])  # kept masked
        self.assertEqual((cmd_path, cmd["name"]), ("/api/v1/command", "ApplicationIndexerSync"))
        with self.assertRaisesRegex(db.LoginError, "^Login failed$"):
            db.ncore_login(self.arr_dir, "654321")

    def test_status_login_checks_and_limits(self):
        status = db.Status(self.arr_dir / "none.ini", self.arr_dir)
        self.assertEqual(status.login("12ab56"), (False, "a 2FA code is 6 digits"))
        self.assertEqual(self.prowlarr.requests, [])
        self.assertEqual(status.login(FakeProwlarr.GOOD_CODE), (True, ""))
        self.assertTrue(status.last_login["ok"])
        for _ in range(db.LOGIN_TRIES - 1):
            self.assertEqual(status.login("654321"), (False, "Login failed"))
        ok, message = status.login(FakeProwlarr.GOOD_CODE)
        self.assertFalse(ok)
        self.assertIn("tries", message)
        self.assertEqual(len([r for r in self.prowlarr.requests if r[0] == "PUT"]), db.LOGIN_TRIES)

    def test_post(self):
        config = write_config(self.arr_dir, "http://unused")
        config.write_text(re.sub(r"(?m)^arr\s*=.*$", "arr = yes", config.read_text()))
        db.Handler.status = db.Status(config, self.arr_dir)
        httpd = db.Server(("127.0.0.1", 0), db.Handler)
        threading.Thread(target=httpd.serve_forever, args=(0.01,), daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        host = f"127.0.0.1:{httpd.server_address[1]}"

        def post(code, origin=None):
            conn = http.client.HTTPConnection(host)
            headers = {"Content-Type": "application/x-www-form-urlencoded"}
            if origin:
                headers["Origin"] = origin
            conn.request("POST", "/ncore-code", f"code={code}", headers)
            resp = conn.getresponse()
            resp.read()
            conn.close()
            return resp.status, resp.headers.get("Location")

        with mock.patch("sys.stderr"):
            self.assertEqual(post("123456", "http://evil.example"), (403, None))
            self.assertEqual(self.prowlarr.requests, [])
            self.assertEqual(post("123+456", f"http://{host}"), (303, "/#ncore"))
            self.assertEqual(self.prowlarr.requests[0][0], "PUT")
            page = db.render(db.Handler.status.get(), "box")
        self.assertIn("logged in with the new code", page)
        config.write_text(config.read_text().replace("arr = yes", "arr = no"))
        self.assertEqual(post("123456")[0], 404)


class LogosTest(unittest.TestCase):
    PNG = b"\x89PNG\r\n\x1a\nfake"

    def test_sources(self):
        arr = FakeArr([])  # answers /ping with "{}": not an SVG
        self.addCleanup(arr.close)
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "icon.abc123.png").write_bytes(self.PNG)
            (Path(tmp) / "not-a.png").write_bytes(b"<html>")
            logos = db.Logos({
                "a": [f"{tmp}/missing.svg", f"{tmp}/icon.*.png"],
                "b": [f"{tmp}/not-a.png", f"http://127.0.0.1:{arr.port}/ping.svg"],
            })
            self.assertEqual(logos.get("a"), (self.PNG, "image/png"))
            self.assertIsNone(logos.get("b"))
            self.assertIsNone(logos.get("nope"))
            with ThreadPoolExecutor(2) as pool:
                self.assertEqual(logos.available(["a", "b"], pool), ["a"])
            # found ones are kept; a missing one is looked for again only after a while
            (Path(tmp) / "icon.abc123.png").unlink()
            self.assertEqual(logos.get("a"), (self.PNG, "image/png"))
            (Path(tmp) / "late.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
            logos.sources["b"].append(f"{tmp}/late.svg")
            self.assertIsNone(logos.get("b"))
            logos.missed["b"] -= db.LOGO_RETRY
            self.assertEqual(logos.get("b")[1], "image/svg+xml")

    def test_tiles_use_logos(self):
        status = {**PageTest.STATUS, "logos": ["transmission"]}
        page = db.render(status, "box")
        self.assertIn('<img class="logo" src="/icon/transmission"', page)
        self.assertNotIn('src="/icon/prowlarr"', page)  # no logo: our glyph
        self.assertIn('class="badge"', page)


class DiskHealthTest(unittest.TestCase):
    LOG = ("2026-10-29 15:00:01;\t1;80;12345678;\t5;100;0;\t194;35;35 (Min/Max 18/45);"
           "\t197;100;0;\t198;100;0;\n")

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.dir = Path(self.tmpdir.name)
        for patch in (mock.patch.object(db, "SMART_DIR", self.dir),
                      mock.patch.object(db, "service_check", RUNNING)):
            patch.start()
            self.addCleanup(patch.stop)
        self.log = self.dir / "attrlog.ST12000VN0008-3MH101-WZ00E5CG.ata.csv"

    def test_healthy(self):
        self.log.write_text(self.LOG.replace("15:00", "14:30") + self.LOG)
        c = db.check_disk({})
        self.assertEqual((c["state"], c["summary"]),
                         (db.OK, "ST12000VN0008-3MH101 35 °C, no bad sectors"))

    def test_bad_sectors_and_heat(self):
        self.log.write_text(self.LOG.replace("\t5;100;0;", "\t5;99;8;").replace("35 (", "57 ("))
        c = db.check_disk({})
        self.assertEqual(c["state"], db.WARN)
        self.assertIn("hot (57 °C)", c["details"][0])
        self.assertIn("8 reallocated sectors", c["details"][1])

    def test_no_readings_and_no_smartd(self):
        self.assertEqual(db.check_disk({})["summary"], "smartd is watching (no readings yet)")
        self.log.write_text(self.LOG)
        self.assertIn("no readings", db.check_disk({}, now=time.time() + 3 * 3600)["summary"])  # stale
        with mock.patch.object(db, "service_check", lambda unit: (db.DOWN, "not installed")):
            self.assertEqual(db.check_disk({})["state"], db.DOWN)


class NtfyCheckTest(unittest.TestCase):
    S = {"ntfy_server": "http://localhost:2586"}

    @mock.patch.object(db, "service_check", RUNNING)
    def test_healthy_and_not(self):
        with mock.patch.object(db, "http_get", lambda url: b'{"healthy":true}'):
            self.assertEqual(db.check_ntfy(self.S)["state"], db.OK)
        with mock.patch.object(db, "http_get", lambda url: b'{"healthy":false}'):
            self.assertEqual(db.check_ntfy(self.S)["state"], db.WARN)

    def test_service_down_and_never_pushed(self):
        with mock.patch.object(db, "service_check", lambda unit: (db.DOWN, "stopped")):
            self.assertEqual(db.check_ntfy(self.S)["state"], db.DOWN)
        w = db.Watcher(delay=0)  # a push about it would have to go through it
        self.assertEqual(w.update({"ntfy": db.result(db.DOWN, "stopped")}, 0), [])


class WatcherTest(unittest.TestCase):
    def test_alerts_after_a_while_once_and_when_back(self):
        w = db.Watcher(delay=600)
        bad = {"jellyfin": db.result(db.DOWN, "not running")}
        good = {"jellyfin": db.result(db.OK, "healthy")}
        self.assertEqual(w.update(bad, 0), [])
        self.assertEqual(w.update(bad, 599), [])
        self.assertEqual([e[:2] for e in w.update(bad, 600)], [("down", "jellyfin")])
        self.assertEqual(w.update(bad, 900), [])  # once per incident
        self.assertEqual([e[:2] for e in w.update(good, 960)], [("back", "jellyfin")])
        self.assertEqual(w.update(good, 2000), [])
        # bad only briefly: nothing at all
        self.assertEqual(w.update(bad, 3000) + w.update(good, 3100), [])
        # a failed push is tried again
        w.update(bad, 4000)
        self.assertEqual(len(w.update(bad, 4600)), 1)
        w.retry("jellyfin")
        self.assertEqual(len(w.update(bad, 4660)), 1)

    def test_what_pushes_on_its_own_is_left_out(self):
        w = db.Watcher(delay=0)
        checks = {"storage": db.result(db.DOWN, "not mounted"),
                  "prowlarr": db.result(db.WARN, "1 issue"),
                  "disk": db.result(db.WARN, "8 reallocated sectors"),
                  "tailscale": db.result(db.WARN, "NeedsLogin")}
        self.assertEqual([e[1] for e in w.update(checks, 0)], ["tailscale"])
        checks["prowlarr"] = db.result(db.DOWN, "not running")
        self.assertEqual([e[1] for e in w.update(checks, 1)], ["prowlarr"])

    def test_push(self):
        with FakeServer() as server, tempfile.TemporaryDirectory() as tmp:
            s = db.load_settings(write_config(Path(tmp), server.base_url))
            c = db.result(db.DOWN, "not running", ["detail"])
            with mock.patch("sys.stderr"):
                self.assertTrue(db.push_event(s, "down", "jellyfin", c))
                self.assertTrue(db.push_event(s, "back", "jellyfin", db.result(db.OK, "healthy")))
            down, back = server.pushes
            self.assertEqual((down["topic"], down["title"], down["message"], down["priority"]),
                             ("test-topic", "Jellyfin is down", "not running\ndetail", 4))
            self.assertEqual(back["title"], "Jellyfin is fine again")
            server.ntfy_down = True
            with mock.patch("sys.stderr"):
                self.assertFalse(db.push_event(s, "down", "jellyfin", c))


class SettingsTest(unittest.TestCase):
    def test_reads_config_and_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            config = write_config(tmp, "http://box:1")
            text = config.read_text()
            for key, value in (("media_server", "jellyfin"), ("samba", "yes"), ("arr", "yes")):
                text = re.sub(rf"(?m)^{key}\s*=.*$", f"{key} = {value}", text)
            config.write_text(text)
            s = db.load_settings(config)
            self.assertEqual(s["storage_root"], tmp / "storage")
            self.assertFalse(s["require_mount"])
            self.assertEqual((s["rpc_url"], s["rpc_user"], s["rpc_password"]),
                             ("http://box:1/transmission/rpc", "user", "pass"))
            self.assertEqual((s["media_server"], s["samba"], s["tailscale"], s["arr"]),
                             ("jellyfin", True, False, True))

    def test_missing_files_give_defaults(self):
        s = db.load_settings(Path("/nonexistent/config.ini"))
        self.assertEqual((s["storage_root"], s["media_server"], s["arr"], s["own_ntfy"]),
                         (Path("/mnt/storage"), "none", False, False))


class ChecksTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def settings(self, base_url="http://127.0.0.1:1", **kw):
        return {**db.load_settings(write_config(self.tmp, base_url, **kw)), "rpc_user": "user"}

    @mock.patch.object(db, "service_check", RUNNING)
    def test_transmission_counts_and_speeds(self):
        with FakeServer() as server:
            server.tr_torrents = [
                torrent("a", 4, down=2 * 1024 ** 2), torrent("b", 6, up=512 * 1024),
                torrent("c", 6, error=2), torrent("d", 0)]
            c = db.check_transmission(self.settings(server.base_url))
        self.assertEqual(c["state"], db.OK)
        self.assertEqual(c["summary"], "1 downloading, 2 seeding, ↓ 2.0 MB/s ↑ 512.0 KB/s")
        self.assertEqual(c["details"], ["1 with a tracker warning"])

    @mock.patch.object(db, "service_check", RUNNING)
    def test_transmission_local_errors_and_bad_login(self):
        with FakeServer() as server:
            server.tr_torrents = [torrent("Big.Movie", 3, error=3, error_string="No space left")]
            c = db.check_transmission(self.settings(server.base_url))
            self.assertEqual((c["state"], c["details"]), (db.WARN, ["Big.Movie: No space left"]))
            c = db.check_transmission(self.settings(server.base_url, rpc_password="wrong"))
            self.assertEqual((c["state"], c["summary"]), (db.WARN, "running, but its RPC says: login refused"))

    def test_transmission_service_down(self):
        with mock.patch.object(db, "service_check", lambda unit: (db.DOWN, "failed")):
            self.assertEqual(db.check_transmission(self.settings())["state"], db.DOWN)

    def test_arr_health(self):
        arr = FakeArr([{"type": "notice", "message": "fyi"},
                       {"type": "error", "message": "Indexer nCore is unavailable"}])
        self.addCleanup(arr.close)
        (self.tmp / "prowlarr").mkdir()
        (self.tmp / "prowlarr/config.xml").write_text("<Config><ApiKey>KEY</ApiKey></Config>")
        with mock.patch.dict(db.ARR_APPS, {"prowlarr": (arr.port, "v1")}):
            c = db.check_arr("prowlarr", self.tmp)
            self.assertEqual((c["state"], c["summary"], c["details"]),
                             (db.WARN, "1 issue", ["Indexer nCore is unavailable"]))
            arr.issues = []
            self.assertEqual(db.check_arr("prowlarr", self.tmp)["summary"], "healthy")
            # no API key yet: just whether it answers
            (self.tmp / "prowlarr/config.xml").unlink()
            self.assertEqual(db.check_arr("prowlarr", self.tmp)["summary"], "running")
            arr.close()
            self.assertEqual(db.check_arr("prowlarr", self.tmp)["state"], db.DOWN)
            arr.close = lambda: None

    def test_rss2transmission(self):
        now = 1_800_000_000
        units = {"rss2transmission.timer": {"ActiveState": "active"},
                 "rss2transmission.service": {"ActiveState": "inactive", "Result": "success",
                                              "ExecMainExitTimestamp": f"@{now - 120}"}}
        with mock.patch.object(db, "unit_state", lambda unit, user: units[unit]):
            c = db.check_rss2transmission({}, now)
            self.assertEqual((c["state"], c["summary"]), (db.OK, "checks the feed every 5 min, last 2 min ago"))
            units["rss2transmission.service"]["Result"] = "exit-code"
            self.assertEqual(db.check_rss2transmission({}, now)["state"], db.WARN)
            units["rss2transmission.service"].update(Result="success", ExecMainExitTimestamp=f"@{now - 3600}")
            self.assertEqual(db.check_rss2transmission({}, now)["summary"], "last run 1 h ago")
            units["rss2transmission.timer"]["ActiveState"] = "inactive"
            self.assertEqual(db.check_rss2transmission({}, now)["state"], db.DOWN)

    def test_storage(self):
        s = self.settings()
        c = db.check_storage(s)
        self.assertEqual(c["state"], db.OK)
        self.assertIn("free of", c["summary"])
        self.assertIn("percent", c)
        s["disk_warn_percent"] = 0
        self.assertEqual(db.check_storage(s)["state"], db.WARN)
        s["require_mount"] = True  # tmp/storage isn't a mount point
        self.assertEqual(db.check_storage(s)["state"], db.DOWN)


class PageTest(unittest.TestCase):
    STATUS = {"time": 0, "hostname": "box", "storage_root": "/mnt/storage", "checks": {
        "storage": db.result(db.OK, "9 TB free", percent=10),
        "transmission": db.result(db.OK, "1 downloading"),
        "prowlarr": db.result(db.WARN, "1 issue", ["<b>nCore</b> is unavailable"]),
        "samba": db.result(db.OK, "running"),
    }}

    def test_link_host(self):
        cases = {"box": "box", "box:80": "box", "192.168.1.10": "192.168.1.10",
                 "box.tail1234.ts.net:8080": "box.tail1234.ts.net", "[fd7a::1]:80": "[fd7a::1]",
                 '"><script>': "fallback", "": "fallback", None: "fallback", "a b": "fallback"}
        for header, host in cases.items():
            with self.subTest(header=header):
                self.assertEqual(db.link_host(header, "fallback"), host)

    def test_render(self):
        page = db.render(self.STATUS, "box.lan")
        self.assertIn('href="http://box.lan:9091/"', page)
        self.assertIn('href="http://box.lan:9696/"', page)
        self.assertNotIn(":8096/", page)  # no Jellyfin check, no tile
        self.assertIn("1 thing needs a look", page)
        self.assertIn("&lt;b&gt;nCore&lt;/b&gt;", page)  # escaped
        self.assertIn("smb://box.lan/storage", page)
        self.assertIn("<title>⚠ box · torrent-box</title>", page)

    def test_update_time_says_its_zone(self):
        # the box's clock is often UTC: a machine-readable time, which the page's script shows
        # in the viewer's zone, and text that names the box's zone for when it can't
        page = db.render({**self.STATUS, "time": 1793380559}, "box")
        local = time.strftime("%H:%M:%S %Z", time.localtime(1793380559))
        self.assertIn(f'<time datetime="2026-10-30T17:15:59Z">{local}</time>', page)
        self.assertIn("localTimes()", page)

    def test_every_row_and_tile_renders(self):
        checks = {cid: db.result(db.OK, "fine") for cid, *_ in db.TILES + db.ROWS}
        page = db.render({**self.STATUS, "checks": checks}, "box")
        for cid, name, *_ in db.TILES + db.ROWS:
            with self.subTest(cid=cid):
                self.assertIn(name, page)

    def test_2fa_form_only_when_a_code_is_needed(self):
        for needs, shown in ((True, True), (False, False)):
            with self.subTest(needs_code=needs):
                ncore = db.result(db.WARN if needs else db.OK, "x", needs_code=needs)
                page = db.render({**self.STATUS, "checks": {"ncore": ncore}}, "box")
                self.assertEqual('action="/ncore-code"' in page, shown)

    def test_all_ok(self):
        status = {**self.STATUS, "checks": {"transmission": db.result(db.OK, "fine")}}
        page = db.render(status, "box")
        self.assertIn("All systems go", page)
        self.assertNotIn('class="banner', page)
        self.assertIn("<title>box · torrent-box</title>", page)

    def test_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = db.Status(Path(tmp) / "config.ini", Path(tmp))
            with mock.patch.object(db, "gather", lambda s, arr_dir, pool, last: dict(self.STATUS)):
                db.Handler.status = status
                httpd = db.Server(("127.0.0.1", 0), db.Handler)
                threading.Thread(target=httpd.serve_forever, args=(0.01,), daemon=True).start()
                self.addCleanup(httpd.server_close)
                self.addCleanup(httpd.shutdown)
                base = f"http://127.0.0.1:{httpd.server_address[1]}"
                with urllib.request.urlopen(base + "/") as resp:
                    self.assertIn("text/html", resp.headers["Content-Type"])
                    self.assertIn('href="http://127.0.0.1:9091/"', resp.read().decode())
                status.logos = db.Logos({"transmission": [str(Path(tmp) / "t.svg")]})
                (Path(tmp) / "t.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
                with urllib.request.urlopen(base + "/icon/transmission") as resp:
                    self.assertEqual(resp.headers["Content-Type"], "image/svg+xml")
                    self.assertIn("default-src 'none'", resp.headers["Content-Security-Policy"])
                with self.assertRaises(urllib.error.HTTPError) as e:
                    urllib.request.urlopen(base + "/icon/sonarr")
                e.exception.close()
                with urllib.request.urlopen(base + "/status.json") as resp:
                    self.assertEqual(json.loads(resp.read())["hostname"], "box")
                with self.assertRaises(urllib.error.HTTPError) as e:
                    urllib.request.urlopen(base + "/../etc/passwd")
                e.exception.close()
                self.assertEqual(e.exception.code, 404)

    def test_whisparr_only_when_set_up(self):
        for whisparr in (False, True):
            s = {**db.load_settings(Path("/nonexistent")), "media_server": "none", "arr": True,
                 "whisparr": whisparr}
            with mock.patch.object(db, "check_arr", lambda name, arr_dir: db.result(db.OK, "x")), \
                    mock.patch.object(db, "check_seerr", lambda s: db.result(db.OK, "x")), \
                    mock.patch.object(db, "check_ncore", lambda *a: db.result(db.OK, "x")), \
                    mock.patch.object(db, "check_storage", lambda s: db.result(db.OK, "x")), \
                    mock.patch.object(db, "check_disk", lambda s: db.result(db.OK, "x")), \
                    mock.patch.object(db, "check_transmission", lambda s: db.result(db.OK, "x")), \
                    mock.patch.object(db, "check_rss2transmission", lambda s: db.result(db.OK, "x")):
                with ThreadPoolExecutor(2) as pool:
                    checks = db.gather(s, Path("/nonexistent"), pool)["checks"]
            self.assertIn("radarr", checks)
            self.assertEqual("whisparr" in checks, whisparr)

    def test_desireerr_comes_with_whisparr(self):
        for whisparr in (False, True):
            s = {**db.load_settings(Path("/nonexistent")), "media_server": "none", "arr": True,
                 "whisparr": whisparr}
            with mock.patch.object(db, "check_arr", lambda name, arr_dir: db.result(db.OK, "x")), \
                    mock.patch.object(db, "check_seerr", lambda s: db.result(db.OK, "x")), \
                    mock.patch.object(db, "check_desireerr", lambda s: db.result(db.OK, "x")), \
                    mock.patch.object(db, "check_ncore", lambda *a: db.result(db.OK, "x")), \
                    mock.patch.object(db, "check_storage", lambda s: db.result(db.OK, "x")), \
                    mock.patch.object(db, "check_disk", lambda s: db.result(db.OK, "x")), \
                    mock.patch.object(db, "check_transmission", lambda s: db.result(db.OK, "x")), \
                    mock.patch.object(db, "check_rss2transmission", lambda s: db.result(db.OK, "x")):
                with ThreadPoolExecutor(2) as pool:
                    checks = db.gather(s, Path("/nonexistent"), pool)["checks"]
            self.assertEqual("desireerr" in checks, whisparr)

    def test_desireerr_check(self):
        cases = [('{"setup_needed": false, "version": "0.1.0"}', db.OK, "running, version 0.1.0"),
                 ('{"setup_needed": true, "version": "0.1.0"}', db.WARN, "waiting for its first sign-in")]
        for body, state, summary in cases:
            with mock.patch.object(db, "http_get", lambda url, body=body: body.encode()):
                r = db.check_desireerr({})
            self.assertEqual((r["state"], r["summary"]), (state, summary))
        with mock.patch.object(db, "http_get", side_effect=ConnectionRefusedError(111, "refused")):
            self.assertEqual(db.check_desireerr({})["state"], db.DOWN)

    def test_a_failing_check_is_reported_not_raised(self):
        s = {**db.load_settings(Path("/nonexistent")), "media_server": "none"}
        def boom(s):
            raise RuntimeError("bug")
        with mock.patch.object(db, "check_storage", boom), \
                mock.patch.object(db, "check_transmission", lambda s: db.result(db.OK, "x")), \
                mock.patch.object(db, "check_rss2transmission", lambda s: db.result(db.OK, "x")):
            with ThreadPoolExecutor(2) as pool:
                checks = db.gather(s, Path("/nonexistent"), pool)["checks"]
        self.assertEqual(checks["storage"]["state"], db.WARN)
        self.assertIn("RuntimeError", checks["storage"]["summary"])


if __name__ == "__main__":
    unittest.main()
