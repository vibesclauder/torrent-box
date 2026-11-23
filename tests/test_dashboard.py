import base64
import http.client
import json
import re
import struct
import tempfile
import threading
import time
import unittest
import unittest.mock
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from helpers import FakeServer, write_config
import dashboard as db

RUNNING = lambda unit: (db.OK, "running")  # noqa: E731
STATUS_OTHERS = db.Status.other_services


def setUpModule():
    # the Status tests mustn't look for the test machine's own web services (OthersTest does)
    patcher = mock.patch.object(db.Status, "other_services", lambda self, checks: [])
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


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
                    body = (b"{}" if self.path == "/ping" else
                            json.dumps({"version": "3.0.2.5780"}).encode()
                            if self.path.endswith("/system/status") else json.dumps(outer.issues).encode())
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


class FakeApp:
    """An app's API: GET replies from `routes` (path without /api/vN -> body), the rest
    recorded in `posts` and answered with {}."""

    def __init__(self, routes):
        self.routes, self.posts = routes, []
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
                path = self.path.split("/", 3)[3]
                if path in outer.routes:
                    return self.reply(200, outer.routes[path])
                self.reply(404, {})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.posts.append((self.path.split("/", 3)[3], body))
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
        with mock.patch.object(db, "research", return_value=["1 movie"]) as research, \
                mock.patch("sys.stderr"):
            self.assertEqual(status.login(FakeProwlarr.GOOD_CODE), (True, ""))
            for _ in range(100):
                if status.last_login.get("searched"):
                    break
                time.sleep(0.01)
        self.assertTrue(status.last_login["ok"])
        self.assertEqual(status.last_login["searched"], ["1 movie"])
        self.assertEqual(research.call_args.args[0], self.arr_dir)
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
        self.log = self.dir / "attrlog.ST12000VN0008_3MH101-ZA0B1C2D.ata.csv"
        # no drive database, no disks: the model as smartd's log name has it
        for patch in (mock.patch.object(db, "DRIVEDBS", []),
                      mock.patch.object(db, "SYS_BLOCK", self.dir / "block"),
                      mock.patch.object(db, "UDEV_DATA", self.dir / "udev")):
            patch.start()
            self.addCleanup(patch.stop)
        db.drive_families.cache_clear()
        self.addCleanup(db.drive_families.cache_clear)

    def test_healthy(self):
        self.log.write_text(self.LOG.replace("15:00", "14:30") + self.LOG)
        c = db.check_disk({})
        self.assertEqual((c["state"], c["summary"]),
                         (db.OK, "ST12000VN0008_3MH101, 35 °C, no bad sectors"))

    def test_name_from_drive_database(self):
        drivedb = self.dir / "drivedb.h"
        drivedb.write_text('''/* drivedb.h */
  { "VERSION: 7.5", "-", "", "", "" },
  { "Seagate IronWolf", // tested with ST12000VN0008-2JH101/SC60, see https://x/
    "ST([123468]|1[0246])000VN00(0?[0234678]|1|22|33|41)-.*",
    "", "",
    "-v 18,raw48,Head_Health "
  },
  { "Western Digital Red (CMR)", "WDC WD[1-8]0EFR" "X-.*", "", "", "" },
  { "USB: Seagate; ", "0x0bc2:.*", "", "", "" },
''')
        block, udev = self.dir / "block", self.dir / "udev"
        for dev, n, serial in (("nvme0n1", "259:0", "1234567890123456"), ("sda", "8:0", "ZA0B1C2D")):
            (block / dev).mkdir(parents=True)
            (block / dev / "dev").write_text(n + "\n")
            (block / dev / "size").write_text("23437770752\n")
            udev.mkdir(exist_ok=True)
            (udev / f"b{n}").write_text(f"S:disk/by-id/x\nE:ID_MODEL=ST12000VN0008-3M\n"
                                        f"E:ID_SERIAL=ST12000VN0008-3MH101_{serial}\n"
                                        f"E:ID_SERIAL_SHORT={serial}\n")
        (block / "sda1").mkdir()  # a partition: the disk's serial too, but not the disk
        (block / "sda1" / "partition").write_text("1\n")
        (block / "sda1" / "dev").write_text("8:1\n")
        (block / "sda1" / "size").write_text("2048\n")
        (udev / "b8:1").write_text("E:ID_SERIAL=ST12000VN0008-3MH101_ZA0B1C2D\n"
                                   "E:ID_SERIAL_SHORT=ZA0B1C2D\n")
        with mock.patch.object(db, "DRIVEDBS", [self.dir / "missing.h", drivedb]):
            db.drive_families.cache_clear()
            self.assertEqual(db.find_disk("ST12000VN0008_3MH101", "ZA0B1C2D"),
                             ("Seagate IronWolf 12 TB", "sda"))
            self.assertEqual(db.drive_family("WDC WD40EFRX-68N32N0"), "Western Digital Red (CMR)")
            self.assertIsNone(db.drive_family("0x0bc2:0x2101"))  # USB bridges aren't drives
            self.assertEqual(db.find_disk("Unknown_Disk", "X1"), ("Unknown_Disk", None))

    def test_reads_and_writes(self):
        (self.dir / "block" / "sdz").mkdir(parents=True)
        stat = self.dir / "block" / "sdz" / "stat"

        def look(now, read, written, busy):
            stat.write_text(f"  5035930 268797 {read} 157383274 708330 213036 {written} "
                            f"70306884 0 {busy} 229276647 0 0 0 0 22761 1586487\n")
            return db.disk_io("sdz", now)
        with mock.patch.object(db, "SYS_BLOCK", self.dir / "block"), \
                mock.patch.dict(db.DISK_IO, clear=True):
            self.assertEqual(look(1000, 0, 0, 0)["read"], None)  # nothing to compare with yet
            io = look(1010, 20480 * 10, 2048 * 10, 2500)  # 10 s: 10 MB/s, 1 MB/s, 2.5 s busy
            self.assertEqual((io["read"], io["write"], io["busy"]), (10 << 20, 1 << 20, 25))
            io = look(1060, 20480 * 60, 2048 * 10, 2500)  # the first minute: 600 MB in 60 s
            self.assertEqual(io["history"], [(1060, 10 << 20, (2048 * 10 * 512) / 60, 2500 / 600)])
            self.assertIsNone(look(1070, 0, 0, 0)["read"])  # counters went back: a reboot?

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

    def test_link_to_the_server(self):
        cases = [("http://localhost:2586", "box.lan", "http://box.lan:2586"),
                 ("http://127.0.0.1:2586", "[fd7a::1]", "http://[fd7a::1]:2586"),
                 ("https://ntfy.example", "box.lan", "https://ntfy.example")]
        for server, host, url in cases:
            self.assertEqual(db.ntfy_link(server, host), url)
        status = {"time": 0, "hostname": "box", "storage_root": "/mnt/storage", "checks": {
            "ntfy": db.result(db.OK, "own server, running", server="http://localhost:2586")}}
        self.assertIn('<a href="http://box.lan:2586/">http://box.lan:2586</a>',
                      db.render(status, "box.lan"))
        w = db.Watcher(delay=0)  # a push about it would have to go through it
        self.assertEqual(w.update({"ntfy": db.result(db.DOWN, "stopped")}, 0), [])

    def test_links_over_https(self):
        status = {"time": 0, "hostname": "box", "storage_root": "/mnt/storage", "checks": {
            "seerr": db.result(db.OK, "running"),
            "ntfy": db.result(db.OK, "own server, running", server="http://localhost:2586")}}
        page = db.render(status, "box.tail1234.ts.net", https=True)
        self.assertIn('href="/open/seerr"', page)  # no name of its own: under our bar here too
        self.assertIn('<a href="https://box.tail1234.ts.net:12586/">', page)
        self.assertIn('href="/open/seerr"', db.render(status, "box"))  # plain http: under our bar
        self.assertEqual(db.ntfy_link("https://ntfy.sh", "box", https=True), "https://ntfy.sh")

    def tailnet_status(self, **tailnet):
        tailnet = db.result(db.OK, "connected", **{"name": "box.tail1234.ts.net", "certs": True,
                                                    "tags": [], "approved": [], "served": {},
                                                    "links": {}, **tailnet})
        return {"time": 0, "hostname": "box", "storage_root": "/mnt/storage", "checks": {
            "seerr": db.result(db.OK, "running"), "desireerr": db.result(db.OK, "running"),
            "tailscale": tailnet}}

    def test_links_to_tailscale_services(self):
        status = self.tailnet_status(links={"5055": "seerr.tail1234.ts.net"})
        page = db.render(status, "torrent-box.tail1234.ts.net", https=True)   # opened by its service name
        # its own name, so a phone opens its installed app; under our bar where that doesn't answer
        self.assertIn('href="https://seerr.tail1234.ts.net/" data-fallback="/open/seerr"', page)
        self.assertIn('href="/open/desireerr"', page)                           # no service: under our bar
        self.assertIn('href="/open/seerr"', db.render(status, "box"))           # plain http: under our bar

    def test_open_page_over_https(self):
        status = self.tailnet_status(links={"5055": "seerr.tail1234.ts.net"})
        page = db.render_open(status, "desireerr", "torrent-box.tail1234.ts.net", https=True)
        self.assertIn('<iframe src="https://box.tail1234.ts.net:15056/"', page)  # the box's name has the ports
        self.assertIn('href="https://box.tail1234.ts.net:15056/" target="_blank"', page)
        self.assertIn('href="https://seerr.tail1234.ts.net/" data-fallback="/open/seerr"', page)
        self.assertIn("useReachable()", page)

    def test_services_note(self):
        status = self.tailnet_status(approved=["seerr", "torrent-box"],
                                     served={"seerr": "seerr.t", "torrent-box": "torrent-box.t"})
        self.assertIn("2 of 3 ready", db.render(status, "box.tail1234.ts.net", https=True))
        self.assertNotIn("ready ·", db.render(status, "box"))                   # LAN viewers: no note
        status["checks"]["tailscale"].update(approved=["seerr", "torrent-box", "desireerr"],
                                             served={"seerr": "s", "torrent-box": "t", "desireerr": "d"})
        self.assertNotIn("ready ·", db.render(status, "box.tail1234.ts.net", https=True))

    def test_tailscale_page(self):
        status = self.tailnet_status(tags=["tag:server"], approved=["seerr"], served={"seerr": "s"})
        page = db.render_tailscale(status, https=True)
        self.assertIn('data-probe="https://seerr.tail1234.ts.net/"', page)      # ready: checked from the device
        self.assertNotIn('data-probe="https://desireerr.tail1234.ts.net/"', page)
        self.assertIn("&quot;svc:desireerr&quot;", page)                        # in the policy to paste
        self.assertIn("[&quot;autogroup:member&quot;], &quot;dst&quot;: [&quot;svc:seerr&quot;]", page)
        self.assertNotIn("autogroup:member&quot;], &quot;dst&quot;: [&quot;svc:torrent-box", page)  # members: Seerr only
        self.assertIn("now: tag:server", page)
        self.assertIn("isn't set up", db.render_tailscale({"hostname": "box", "checks": {}}))

    def test_approved_services_only(self):
        me = {"CapMap": {"service-host": [{"svc:seerr": ["100.1.2.3"]}]}}
        serving = {"Services": {
            "svc:seerr": {"Web": {"seerr.t.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:5055"}}}}},
            "svc:jellyfin": {"Web": {"jellyfin.t.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8096"}}}}}}}
        with unittest.mock.patch.object(db.subprocess, "run",
                                        return_value=unittest.mock.Mock(stdout=json.dumps(serving))):
            self.assertEqual(db.tailscale_services(me), {
                "approved": ["seerr"], "served": {"seerr": "seerr.t.ts.net", "jellyfin": "jellyfin.t.ts.net"},
                "links": {"5055": "seerr.t.ts.net"}})

    def test_version_in_the_footer(self):
        status = {"time": 0, "hostname": "box", "storage_root": "/mnt/storage", "checks": {}}
        self.assertIn("<span>torrent-box · <a", db.render(status, "box"))  # not installed
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(db, "VERSION", "1.0 rev 2"), \
                mock.patch.object(db, "INSTALLED_FILE", Path(tmp) / "installed.json"):
            self.assertIn("<span>torrent-box 1.0 rev 2 · <a", db.render(status, "box"))
            db.INSTALLED_FILE.write_text('{"commit": "abc", "date": "2026-11-09 13:31", "repo": "/r"}')
            self.assertIn("<span>torrent-box 1.0 rev 2, 2026-11-09 13:31 · <a",
                          db.render(status, "box"))


class WebPushTest(unittest.TestCase):
    ENDPOINT = "https://fcm.googleapis.com/fcm/send/abc"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.push = db.WebPush(self.tmp.name)

    def browser(self, endpoint=None):
        """A browser's side of a subscription: its keys, as the page posts them."""
        c = db.push_crypto()
        ec, sr = c["ec"], c["serialization"]
        key = ec.generate_private_key(ec.SECP256R1())
        auth = bytes(range(16))
        pub = key.public_key().public_bytes(sr.Encoding.X962, sr.PublicFormat.UncompressedPoint)
        return key, auth, {"endpoint": endpoint or self.ENDPOINT,
                           "keys": {"p256dh": db.b64url(pub), "auth": db.b64url(auth)}}

    def test_only_known_push_services(self):
        _, _, sub = self.browser()
        self.push.subscribe(dict(sub, label="  My   phone "))
        self.assertEqual([x["label"] for x in self.push.subs], ["My phone"])
        for bad in ("http://fcm.googleapis.com/x", "https://localhost/x", "https://192.168.1.2/x",
                    "https://fcm.googleapis.com.evil.example/x", "https://u@fcm.googleapis.com/x",
                    "https://fcm.googleapis.com:8443/x", "https://[::1/x"):
            with self.assertRaises(ValueError, msg=bad):
                self.push.subscribe(dict(sub, endpoint=bad))
        with self.assertRaises(ValueError):
            self.push.subscribe({"endpoint": self.ENDPOINT, "keys": {"p256dh": "x", "auth": "y"}})

    def test_subscriptions_survive_a_restart_and_the_limit_holds(self):
        _, _, sub = self.browser()
        self.push.subscribe(sub)
        self.push.subscribe(sub)  # again: still one
        self.assertEqual(len(db.WebPush(self.tmp.name).subs), 1)
        self.assertEqual(db.WebPush(self.tmp.name).token, self.push.token)
        self.assertEqual(db.WebPush(self.tmp.name).public_key(), self.push.public_key())
        for i in range(db.PUSH_DEVICES - 1):
            self.push.subscribe(self.browser(f"https://fcm.googleapis.com/{i}")[2])
        with self.assertRaises(ValueError):
            self.push.subscribe(self.browser("https://fcm.googleapis.com/more")[2])
        self.push.unsubscribe(self.ENDPOINT)
        self.assertEqual(len(self.push.subs), db.PUSH_DEVICES - 1)

    def test_encrypted_the_way_a_browser_decrypts(self):
        key, auth, sub = self.browser()
        body = self.push.encrypt({"p256dh": sub["keys"]["p256dh"], "auth": sub["keys"]["auth"]}, b"hi")
        c = db.push_crypto()
        ec, hashes, sr = c["ec"], c["hashes"], c["serialization"]
        salt, idlen = body[:16], body[20]
        as_public, sealed = body[21:21 + idlen], body[21 + idlen:]
        ua = key.public_key().public_bytes(sr.Encoding.X962, sr.PublicFormat.UncompressedPoint)
        shared = key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public))
        ikm = c["HKDF"](hashes.SHA256(), 32, auth, b"WebPush: info\0" + ua + as_public).derive(shared)
        cek = c["HKDF"](hashes.SHA256(), 16, salt, b"Content-Encoding: aes128gcm\0").derive(ikm)
        nonce = c["HKDF"](hashes.SHA256(), 12, salt, b"Content-Encoding: nonce\0").derive(ikm)
        self.assertEqual(c["AESGCM"](cek).decrypt(nonce, sealed, None), b"hi\x02")

    def test_a_push_goes_to_every_device_and_a_forgotten_one_is_dropped(self):
        for i in range(2):
            self.push.subscribe(self.browser(f"https://fcm.googleapis.com/{i}")[2])
        gone = db.urllib.error.HTTPError(self.ENDPOINT, 410, "Gone", {}, None)
        calls = []

        def fake(req, timeout):
            calls.append(req.full_url)
            if req.full_url.endswith("/1"):
                raise gone
            return mock.MagicMock()
        with mock.patch.object(db.urllib.request, "urlopen", fake):
            self.assertEqual(self.push.send("Jellyfin is down", "not running", wait=True), 1)
        self.assertEqual(len(calls), 2)
        self.assertEqual([x["endpoint"] for x in self.push.subs], ["https://fcm.googleapis.com/0"])

    def test_send_push_reaches_browsers_with_or_without_ntfy(self):
        sent = []
        push = mock.MagicMock(send=lambda *a, **k: sent.append(a))
        with mock.patch.object(db, "WEBPUSH", push):
            s = {"ntfy_topic": "", "ntfy_server": "x"}
            self.assertTrue(db.send_push(s, "T", "M", 3, []))
        self.assertEqual(sent, [("T", "M")])

    def test_test_push_is_rate_limited(self):
        with self.assertRaises(ValueError):
            self.push.test(now=100)  # no one subscribed
        self.push.subscribe(self.browser()[2])
        with mock.patch.object(self.push, "deliver", lambda sub, data: ""):
            self.push.test(now=100)
            with self.assertRaises(ValueError):
                self.push.test(now=105)
            self.push.test(now=111)

    def test_the_calls(self):
        config = write_config(Path(self.tmp.name), "http://127.0.0.1:9")
        db.Handler.status = db.Status(config, Path(self.tmp.name) / "arr")
        db.Handler.webpush = self.push
        httpd = db.Server(("127.0.0.1", 0), db.Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)

        def call(path, body=None, headers=None, method="POST"):
            conn = http.client.HTTPConnection(*httpd.server_address)
            conn.request(method, path, json.dumps(body or {}) if method == "POST" else None, headers or {})
            r = conn.getresponse()
            return r.status, r.read()
        ours = {"X-Requested-With": "torrent-box"}
        _, _, sub = self.browser()
        self.assertEqual(call("/push/subscribe", sub)[0], 403)            # another site's form
        self.assertEqual(call("/push/subscribe", dict(sub, endpoint="https://evil.example/"), ours)[0], 400)
        self.assertEqual(call("/push/subscribe", sub, ours)[0], 200)
        info = json.loads(call("/push.json", method="GET")[1])
        self.assertEqual((info["supported"], info["devices"], info["key"]), (True, 1, self.push.public_key()))
        self.assertIn(b"showNotification", call("/sw.js", method="GET")[1])
        # the root jobs: only with the token
        sent = []
        with mock.patch.object(self.push, "send", lambda *a, **k: sent.append(a) or 1):
            self.assertEqual(call("/push/notify", {"title": "Disk problem", "message": "m"})[0], 403)
            self.assertEqual(call("/push/notify", {"title": "Disk problem", "message": "m"},
                                  {"Authorization": "Bearer nope"})[0], 403)
            self.assertEqual(call("/push/notify", {"title": "Disk problem", "message": "m"},
                                  {"Authorization": f"Bearer {self.push.token}"})[0], 200)
        self.assertEqual(sent, [("Disk problem", "m", "")])
        # Prowlarr's webhook: the same token, and its events worded as pushes
        sent.clear()
        basic = base64.b64encode(f"torrent-box:{self.push.token}".encode()).decode()
        auth = {"Authorization": f"Basic {basic}"}  # what Prowlarr's webhook sends
        with mock.patch.object(self.push, "send", lambda *a, **k: sent.append(a) or 1):
            self.assertEqual(call("/push/prowlarr", {"eventType": "Health"})[0], 403)
            for event in ({"eventType": "Health", "level": "error", "message": "nCore is down"},
                          {"eventType": "HealthRestored", "message": "nCore is back"},
                          {"eventType": "Grab", "release": {"releaseTitle": "Some.Show.S01", "indexer": "nCore"}},
                          {"eventType": "ApplicationUpdate"}):
                self.assertEqual(call("/push/prowlarr", event, auth)[0], 200)
        self.assertEqual(sent, [("Prowlarr has a problem", "nCore is down", "prowlarr-health"),
                                ("Prowlarr is fine again", "nCore is back", "prowlarr-health"),
                                ("Added", "Some.Show.S01\nvia nCore", "")])
        self.assertEqual(call("/push/unsubscribe", {"endpoint": self.ENDPOINT}, ours)[0], 200)
        self.assertEqual(self.push.subs, [])


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
        self.assertEqual(c["version"], "4.1.1 (56442e2929)")
        self.assertEqual(db.tile_line(c), "↓2.0 MB/s ↑512 kB/s")
        self.assertEqual(db.tile_line({**c, "rate_down": 0, "rate_up": 0}), "idle")
        self.assertEqual(db.tile_line({**c, "rate_down": 250 * 1024 ** 2, "rate_up": 100 * 1024 ** 2}),
                         "↓250 MB/s ↑100 MB/s")  # a 2 Gbit/s line, flat out: still fits
        self.assertEqual(db.tile_line({**c, "rate_down": 8.4 * 1024 ** 2, "rate_up": 300}),
                         "↓8.4 MB/s ↑300 B/s")
        self.assertEqual((c["details"], c["upstream"]), ([], ["1 with a tracker warning"]))

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
            c = db.check_arr("prowlarr", self.tmp)
            self.assertEqual((c["summary"], c["version"]), ("healthy", "3.0.2.5780"))
            # no API key yet: just whether it answers
            (self.tmp / "prowlarr/config.xml").unlink()
            self.assertEqual(db.check_arr("prowlarr", self.tmp)["summary"], "running")
            arr.close()
            self.assertEqual(db.check_arr("prowlarr", self.tmp)["state"], db.DOWN)
            arr.close = lambda: None

    def test_arr_issues_from_upstream_dont_warn(self):
        rate_limited = {"source": "IndexerStatusCheck", "type": "warning",
                        "message": "Indexers unavailable due to failures: nCore (Prowlarr)"}
        no_search = {"source": "IndexerSearchCheck", "type": "warning",
                     "message": "All search-capable indexers are temporarily unavailable "
                                "due to recent indexer errors"}
        root = {"source": "RootFolderCheck", "type": "error", "message": "Missing root folder"}
        arr = FakeArr([rate_limited, no_search])
        self.addCleanup(arr.close)
        for app in ("sonarr", "prowlarr"):
            (self.tmp / app).mkdir()
            (self.tmp / app / "config.xml").write_text("<Config><ApiKey>KEY</ApiKey></Config>")
        with mock.patch.dict(db.ARR_APPS, {"sonarr": (arr.port, "v3"), "prowlarr": (arr.port, "v1")}):
            c = db.check_arr("sonarr", self.tmp)
            self.assertEqual((c["state"], c["summary"]), (db.OK, "healthy"))
            self.assertEqual((c["details"], len(c["upstream"])), ([], 2))
            self.assertTrue(c["upstream"][0].startswith("Indexers unavailable"))
            # its indexers are Prowlarr's own business
            self.assertEqual(db.check_arr("prowlarr", self.tmp)["state"], db.WARN)
            arr.issues = [rate_limited, root]
            c = db.check_arr("sonarr", self.tmp)
            self.assertEqual((c["state"], c["summary"]), (db.WARN, "1 issue"))
            self.assertEqual((c["details"], c["upstream"]), (["Missing root folder"], [rate_limited["message"]]))

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


def stamp(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


class ResearchTest(unittest.TestCase):
    """After a new nCore code: searching again for what the apps wanted meanwhile."""

    NOW = 1_800_000_000
    SINCE = NOW - 3600

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.arr_dir = Path(self.tmpdir.name)
        self.apps = {}

    def app(self, name, routes):
        (self.arr_dir / name).mkdir()
        (self.arr_dir / name / "config.xml").write_text("<Config><ApiKey>KEY</ApiKey></Config>")
        self.apps[name] = app = FakeApp(routes)
        self.addCleanup(app.close)
        patch = mock.patch.dict(db.ARR_APPS, {name: (app.port, "v1" if name == "prowlarr" else "v3")})
        patch.start()
        self.addCleanup(patch.stop)
        return app

    def test_movies(self):
        new, old = stamp(self.NOW - 600), stamp(self.NOW - 86400)
        movie = {"monitored": True, "hasFile": False, "isAvailable": True, "added": old}
        radarr = self.app("radarr", {"health": [], "indexer": [], "movie": [
            dict(movie, id=1, added=new),                      # requested meanwhile
            dict(movie, id=2),                                 # earlier: searched then
            dict(movie, id=3, digitalRelease=new),             # came out meanwhile
            dict(movie, id=4, added=new, hasFile=True),
            dict(movie, id=5, added=new, monitored=False),
            dict(movie, id=6, added=new, isAvailable=False),   # not out yet
            dict(movie, id=7, digitalRelease=stamp(self.NOW + 86400))]})
        self.assertEqual(db.research(self.arr_dir, self.SINCE, now=self.NOW), ["2 movies"])
        self.assertEqual(radarr.posts, [("command", {"name": "MoviesSearch", "movieIds": [1, 3]})])

    def test_newest_first_and_capped(self):
        whisparr = self.app("whisparr", {"health": [], "indexer": [], "movie": [
            {"id": i, "monitored": True, "hasFile": False, "added": stamp(self.SINCE + i)}
            for i in range(1, db.RESEARCH_MAX + 6)]})
        self.assertEqual(db.research(self.arr_dir, self.SINCE, now=self.NOW),
                         [f"{db.RESEARCH_MAX} scenes"])
        ids = whisparr.posts[0][1]["movieIds"]
        self.assertEqual((len(ids), ids[0]), (db.RESEARCH_MAX, db.RESEARCH_MAX + 5))

    def test_series(self):
        new, old = stamp(self.NOW - 600), stamp(self.NOW - 86400)
        sonarr = self.app("sonarr", {"health": [], "indexer": [], (
            "wanted/missing?page=1&pageSize=1000&monitored=true&includeSeries=true"
            "&sortKey=airDateUtc&sortDirection=descending"): {"records": [
                {"id": 10, "seriesId": 1, "airDateUtc": old, "series": {"added": new}},
                {"id": 11, "seriesId": 1, "airDateUtc": old, "series": {"added": new}},
                {"id": 20, "seriesId": 2, "airDateUtc": new, "series": {"added": old}},
                {"id": 21, "seriesId": 2, "airDateUtc": old, "series": {"added": old}}]}})
        self.assertEqual(db.research(self.arr_dir, self.SINCE, now=self.NOW),
                         ["1 series", "1 episode"])
        self.assertEqual(sonarr.posts, [("command", {"name": "SeriesSearch", "seriesId": 1}),
                                        ("command", {"name": "EpisodeSearch", "episodeIds": [20]})])

    def test_indexer_left_out_after_failures_is_tested_first(self):
        indexer = {"id": 1, "name": "nCore (Prowlarr)", "enableRss": True}
        radarr = self.app("radarr", {"indexer": [indexer], "movie": [], "health": [
            {"source": "IndexerStatusCheck", "type": "error",
             "message": "All indexers are unavailable due to failures"}]})
        self.assertEqual(db.research(self.arr_dir, self.SINCE, now=self.NOW), [])
        self.assertEqual(radarr.posts, [("indexer/test", indexer)])
        radarr.routes["health"], radarr.posts[:] = [], []
        db.research(self.arr_dir, self.SINCE, now=self.NOW)
        self.assertEqual(radarr.posts, [])  # healthy: no test

    def test_waits_for_prowlarrs_sync_and_reports_failures(self):
        prowlarr = self.app("prowlarr", {"command/5": {"id": 5, "status": "completed"}})
        self.app("radarr", {"health": []})  # no /movie
        self.assertEqual(db.research(self.arr_dir, self.SINCE, {"id": 5}, now=self.NOW),
                         ["Radarr failed: HTTP 404"])
        self.assertEqual(prowlarr.posts, [])
        # not set up at all: left out
        self.assertEqual(db.research(Path(self.tmpdir.name) / "none", self.SINCE), [])

    def test_outage_start(self):
        prowlarr = self.app("prowlarr", {"indexerstatus": [
            {"indexerId": 7, "initialFailure": stamp(self.NOW - 7200)}]})
        self.assertEqual(db.outage_start(self.arr_dir, self.NOW),
                         self.NOW - 7200 - db.RESEARCH_MARGIN)
        prowlarr.routes["indexerstatus"] = [{"indexerId": 7, "initialFailure": stamp(self.NOW - 30 * 86400)}]
        self.assertEqual(db.outage_start(self.arr_dir, self.NOW), self.NOW - db.RESEARCH_LONGEST)
        prowlarr.routes["indexerstatus"] = []  # switched off, or it doesn't remember
        self.assertEqual(db.outage_start(self.arr_dir, self.NOW), self.NOW - db.RESEARCH_DEFAULT)

    def test_page_says_what_was_searched(self):
        now = time.time()
        for searched, text in ((None, "searching again"), ([], "nothing was waiting"),
                               (["1 movie"], "searched again: 1 movie")):
            with self.subTest(searched=searched), mock.patch.object(
                    db, "find_ncore", side_effect=db.LoginError("x")):
                login = {"time": now, "ok": True, "message": "", "searched": searched}
                self.assertIn(text, " ".join(db.check_ncore(self.arr_dir, login, now)["details"]))


class SystemTest(unittest.TestCase):
    def test_cpu_between_looks_and_the_hourly_history(self):
        d = db.Deltas()
        self.assertIsNone(d.update(0, (100, 1000), 30))  # nothing to compare with yet
        self.assertEqual(d.update(5, (150, 1100), 30), (5, [50, 100]))
        self.assertEqual(db.cpu_percent([50, 100]), 50)
        self.assertIsNone(db.cpu_percent([0, 0]))  # /proc/stat didn't move
        d.update(30, (160, 1200), 31)  # too soon for a sample
        d.update(60, (225, 1500), 32)  # the minute: 125 of 500
        self.assertEqual(d.series(), [(60, 60, [125, 500], 32)])
        for minute in range(2, 70):
            d.update(minute * 60, (225 + minute, 1500 + minute * 100))
        self.assertEqual(len(d.series()), db.Deltas.KEEP)  # an hour

    def test_memory_adds_up(self):
        meminfo = "MemTotal:  8000000 kB\nMemFree:  1000000 kB\nMemAvailable:  5000000 kB\n"
        with mock.patch.object(db.Path, "read_text", lambda self: meminfo):
            mem = db.memory_info()
        self.assertEqual((mem["used"], mem["cache"], mem["free"]),
                         (3000000 * 1024, 4000000 * 1024, 1000000 * 1024))
        self.assertEqual(mem["used"] + mem["cache"] + mem["free"], mem["total"])

    def test_check(self):
        c = db.check_system({})
        self.assertEqual(c["state"], db.OK)
        self.assertIn("CPUs", c["summary"])
        self.assertTrue({"cpus", "load", "memory", "disk", "history"} <= c["metrics"].keys())

    def test_meters(self):
        gb = 1 << 30
        metrics = {"cpus": 4, "uptime": 90000, "load": 0.5, "cpu": 12.4, "temp": 41.2,
                   "memory": {"total": 8 * gb, "used": 2 * gb, "cache": 5 * gb, "free": gb},
                   "disk": {"used": 20 * gb, "free": 80 * gb, "percent": 20},
                   "history": [(3600 - 120, 3.0, 25.0), (3600 - 60, 40.0, 26.0)]}
        page = db.system_html(db.result(db.OK, "up 1 day 1 h", metrics=metrics), 3600)
        # closed: the summary; open: the meters
        self.assertTrue(page.startswith('<details class="more" data-fold="more-system">'
                                        '<summary>up 1 day 1 h<span class="chev">'))
        for text in ("<b>12%</b>", "load 0.50 · 4 CPUs", "peak 40%", "<b>25%</b>",
                     "2.0 / 8.0 GB · cache 5.0 GB", 'class="cache" style="width:62.5%"',
                     "<b>20%</b>", "80.0 GB free of 100 GB"):
            with self.subTest(text=text):
                self.assertNotIn(text, page[:page.index("</summary>")])
                self.assertIn(text, page)
        self.assertEqual(page.count("<svg"), 2)  # CPU and memory, the last hour
        page = db.system_html(db.result(db.OK, "x", metrics={**metrics, "history": []}), 3600)
        self.assertNotIn("<svg", page)
        self.assertIn("the last hour shows up here", page)
        self.assertEqual(db.system_html(db.result(db.OK, "up 1 h"), 0), "up 1 h")  # no metrics

    def test_size_of(self):
        gb = 1 << 30
        self.assertEqual(db.size_of(4.3 * gb, 12.9 * gb), "4.3 / 12.9 GB")
        self.assertEqual(db.size_of(512 << 20, 2 * gb), "512 MB / 2.0 GB")
        self.assertEqual(db.short_size(1011.6 * (1 << 20)), "1.0 GB")  # not 1011.6 MB
        self.assertEqual(db.short_size(863.2 * gb), "863 GB")
        self.assertEqual(db.short_size(12 * gb * 1024), "12.0 TB")

    def test_disk_io(self):
        mb = 1 << 20
        disk = {"name": "IronWolf", "read": 85 * mb, "write": 0.0, "busy": 34.2,
                "history": [(3480, 2 * mb, 0, 3), (3540, 180 * mb, 12 * mb, 80)]}
        page = db.disk_io_html(db.result(db.OK, "IronWolf 12 TB", io=[disk]), 3600)
        self.assertIn('<summary>IronWolf 12 TB · busy 34%<span class="chev">', page)
        for text in ("<b>85 MB/s</b>", "peak 180 MB/s", "<b>0 B/s</b>", "peak 12 MB/s"):
            with self.subTest(text=text):
                self.assertIn(text, page)
        self.assertEqual(page.count("<svg"), 2)
        idle = db.disk_io_html(db.result(db.OK, "x", io=[{**disk, "busy": 0.1}]), 3600)
        self.assertIn("<summary>x · idle<", idle)
        first = db.disk_io_html(db.result(db.OK, "x", io=[
            {**disk, "read": None, "write": None, "busy": None, "history": []},
            {**disk, "name": "Red"}]), 3600)
        self.assertIn("<summary>x<", first)  # two disks: each line says which
        self.assertIn('<div class="muted">IronWolf</div>', first)  # measuring
        self.assertIn('<div class="muted">Red · busy 34%</div>', first)
        self.assertIn("<b>–</b>", first)
        self.assertEqual(db.disk_io_html(db.result(db.OK, "a & b"), 0), "a &amp; b")  # no I/O


class OthersTest(unittest.TestCase):
    """Other services: the box's web services besides torrent-box's."""

    def test_listening(self):
        with tempfile.TemporaryDirectory() as tmp:
            head = "  sl  local_address rem_address   st\n"
            Path(tmp, "tcp").write_text(head +
                "   0: 00000000:1F90 00000000:0000 0A x\n"     # 0.0.0.0:8080, listening
                "   1: 0100007F:1FA4 00000000:0000 0A x\n"     # 127.0.0.1:8100
                "   2: 3201A8C0:2008 00000000:0000 0A x\n"     # 192.168.1.50:8200
                "   3: 0100007F:0050 0200007F:D431 01 x\n")    # a connection, not listening
            Path(tmp, "tcp6").write_text(head +
                "   0: 00000000000000000000000000000000:15F3 00000000000000000000000000000000:0000 0A x\n"
                "   1: 00000000000000000000000001000000:1F40 00000000000000000000000000000000:0000 0A x\n")
            self.assertEqual(db.listening(Path(tmp)), {
                ("0.0.0.0", 8080), ("127.0.0.1", 8100), ("192.168.1.50", 8200),
                ("::", 5619), ("::1", 8000)})
            self.assertEqual(db.listening(Path(tmp, "none")), set())

    def test_one_entry_per_app_and_only_reachable_ones(self):
        pages = {8100: ("Pantry", "aaa", ["/icon.svg"]), 8200: ("Pantry", "aaa", ["/icon.svg"]),
                 5173: ("Pantry", "bbb", []), 8000: ("Dev API", "ccc", []), 9000: ("", "ddd", []),
                 9091: ("Transmission", "eee", []),
                 # turned us away (a Tailscale identity check): joins its app by name, or stands alone
                 8300: ("Bank", None, []), 8400: ("Bank", "fff", ["/b.svg"]), 8500: ("Vault", None, [])}
        probed = []

        def probe(address, port):
            probed.append((address, port))
            return pages.get(port)

        sockets = {("127.0.0.1", 8100), ("192.168.1.50", 8200), ("127.0.0.1", 5173),
                   ("127.0.0.1", 8000), ("::", 9000), ("0.0.0.0", 9091), ("0.0.0.0", 22),
                   ("100.101.102.103", 18100), ("fe80::1", 5357), ("0.0.0.0", 7000),
                   ("127.0.0.1", 8300), ("192.168.1.50", 8400), ("127.0.0.1", 8500)}
        https = {8100: ["https://box.ts.net:18100/", "https://pantry.ts.net/"],
                 8300: ["https://bank.ts.net/"], 8500: ["https://vault.ts.net/"],
                 5173: ["https://box.ts.net:8443/"]}
        icons = []

        def icon(address, port, paths):
            icons.append((address, port, paths))
            return (b"<svg/>", "image/svg+xml") if paths else None

        found = db.other_services({9091}, sockets, https, probe, icon)
        vault = found.pop()
        self.assertEqual(vault["name"], "Vault")
        self.assertEqual(vault["https"], ["https://vault.ts.net/"])
        self.assertEqual(len(vault["id"]), 16)
        self.assertEqual(found, [
            {"name": "Bank", "ports": [8300, 8400], "lan": {"8400": "192.168.1.50"},
             "https": ["https://bank.ts.net/"], "id": "fff", "picture": (b"<svg/>", "image/svg+xml")},
            {"name": "Pantry", "ports": [5173], "lan": {}, "https": ["https://box.ts.net:8443/"],
             "id": "bbb", "picture": None},
            {"name": "Pantry", "ports": [8100, 8200], "lan": {"8200": "192.168.1.50"},
             "https": ["https://pantry.ts.net/", "https://box.ts.net:18100/"],
             "id": "aaa", "picture": (b"<svg/>", "image/svg+xml")},
            {"name": "Port 9000", "ports": [9000], "lan": {"9000": ""}, "https": [], "id": "ddd",
             "picture": None},
        ])
        self.assertIn(("127.0.0.1", 8100, ["/icon.svg"]), icons)  # where the page answered
        self.assertEqual(len(icons), 5)  # once per app
        ports = {port for _, port in probed}
        self.assertNotIn(8000, ports)   # on 127.0.0.1 alone, not served on the tailnet
        self.assertNotIn(9091, ports)   # a tile's
        self.assertNotIn(22, ports)     # never a web page
        self.assertNotIn(18100, ports)  # tailscaled's own
        self.assertNotIn(5357, ports)   # link-local
        self.assertIn(("127.0.0.1", 9000), probed)  # on all addresses: tried on this box

    def test_web_page(self):
        class App(BaseHTTPRequestHandler):
            server_version = "App"
            start = "/login"  # where / sends the browser

            def log_message(self, *args):
                pass

            def do_GET(self):
                routes = {"/": (302, "", b"", self.start),
                          "/login": (200, "text/html", b"<html><TITLE>\n My &amp; App </title>"
                                     b'<link rel="icon" href="img/fav.png" sizes="32x32">', ""),
                          "/api": (200, "application/json", b"{}", ""),
                          "/locked": (403, "text/html", b"<title>My App</title><p>Not allowed", ""),
                          "/gone": (404, "text/html", b"<title>Error response</title>", "")}
                status, kind, body, location = routes[self.path]
                self.send_response(status)
                if kind:
                    self.send_header("Content-Type", kind)
                if location:
                    self.send_header("Location", location)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), App)
        threading.Thread(target=httpd.serve_forever, args=(0.01,), daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        port = httpd.server_address[1]
        title, digest, icons = db.web_page("127.0.0.1", port)
        self.assertEqual(title, "My & App")
        self.assertEqual(len(digest), 16)
        self.assertEqual(icons, ["/img/fav.png", "/favicon.ico"])  # relative to the login page
        App.start = "/locked"  # the app, turning us away: no digest to match it by
        self.assertEqual(db.web_page("127.0.0.1", port), ("My App", None, ["/favicon.ico"]))
        for path in ("/api", "/gone"):  # not a page; an error page isn't the app
            with self.subTest(path=path):
                App.start = path
                self.assertIsNone(db.web_page("127.0.0.1", port))
        self.assertIsNone(db.web_page("127.0.0.1", 1))  # nothing there

        App.server_version = db.Handler.server_version  # this page itself
        self.assertIsNone(db.web_page("127.0.0.1", port))

    def test_icon_links(self):
        page = (b'<html><head><link rel="stylesheet" href="/a.css">'
                b'<link rel="icon" href="/favicon-16.png" sizes="16x16">'
                b'<link rel="shortcut icon" href="/favicon-32.png" sizes="32x32">'
                b'<link rel="apple-touch-icon" href="/touch.png">'
                b'<link rel="icon" href="https://cdn.example/x.svg">'
                b'<link rel="icon" href="//cdn.example/y.png">'
                b'<link rel="icon" type="image/svg+xml" href="logo">'
                b'<link rel="ICON" href="/favicon.ico"></head>')
        self.assertEqual(db.IconLinks.paths(page, "/app/"), [
            "/app/logo", "/touch.png", "/favicon-32.png", "/favicon-16.png", "/favicon.ico"])
        self.assertEqual(db.IconLinks.paths(b"<html><title>x"), ["/favicon.ico"])

    def test_picture_type(self):
        cases = {b"\x89PNG\r\n": "image/png", b"\x00\x00\x01\x00\x01": "image/x-icon",
                 b"\xff\xd8\xff": "image/jpeg", b"GIF89a": "image/gif",
                 b"RIFF\x00\x00\x00\x00WEBPVP8": "image/webp",
                 b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"/>': "image/svg+xml",
                 b"<!doctype html><html><svg></svg>": None, b"{}": None}
        for data, kind in cases.items():
            with self.subTest(data=data):
                self.assertEqual(db.picture_type(data), kind)

    def test_fetch_icon(self):
        class App(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                files = {"/page.png": b"<html>not found, but 200</html>", "/favicon.ico": b"\x00\x00\x01\x00ico",
                         "/huge.png": b"\x89PNG" + b"x" * db.LOGO_MAX}
                body = files.get(self.path, b"")
                self.send_response(200 if body else 404)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), App)
        threading.Thread(target=httpd.serve_forever, args=(0.01,), daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        port = httpd.server_address[1]
        self.assertEqual(db.fetch_icon("127.0.0.1", port, ["/none.svg", "/page.png", "/huge.png", "/favicon.ico"]),
                         (b"\x00\x00\x01\x00ico", "image/x-icon"))
        self.assertIsNone(db.fetch_icon("127.0.0.1", port, ["/none.svg"]))
        self.assertIsNone(db.fetch_icon("127.0.0.1", 1, ["/favicon.ico"]))

    def test_tailscale_https(self):
        serving = {"Web": {"box.ts.net:18100": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8100"}}},
                           "box.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:80"}}},
                           "box.ts.net:9443": {"Handlers": {"/": {"Path": "/srv"}}}},
                   "Services": {"svc:pantry": {"Web": {"pantry.ts.net:443": {
                       "Handlers": {"/": {"Proxy": "http://localhost:8100/"}}}}}}}
        done = mock.Mock(stdout=json.dumps(serving))
        with mock.patch.object(db.subprocess, "run", return_value=done):
            self.assertEqual(db.tailscale_https(), {
                8100: ["https://pantry.ts.net/", "https://box.ts.net:18100/"],
                80: ["https://box.ts.net/"]})
        with mock.patch.object(db.subprocess, "run", side_effect=FileNotFoundError):
            self.assertEqual(db.tailscale_https(), {})

    def test_known_ports(self):
        checks = {"transmission": {}, "seerr": {}, "ntfy": {"server": "http://localhost:2586"},
                  "storage": {}}
        self.assertEqual(db.known_ports(checks), {9091, 5055, 2586})

    def test_links(self):
        pantry = {"name": "Pantry", "ports": [8100, 8200], "lan": {"8200": "192.168.1.50"},
                 "https": ["https://pantry.ts.net/", "https://box.ts.net:18100/"]}
        everywhere = {"name": "X", "ports": [8200, 9000], "lan": {"8200": "192.168.1.50", "9000": ""},
                      "https": []}
        tailnet_only = {"name": "Dev", "ports": [5173], "lan": {}, "https": ["https://box.ts.net:8443/"]}
        self.assertEqual(db.other_url(pantry, "box.lan"), "http://192.168.1.50:8200/")
        self.assertEqual(db.other_url(pantry, "box.ts.net", https=True), "https://pantry.ts.net/")
        self.assertEqual(db.other_url(everywhere, "box.lan"), "http://box.lan:9000/")
        self.assertEqual(db.other_url(everywhere, "[fd7a::1]"), "http://[fd7a::1]:9000/")
        self.assertEqual(db.other_url(tailnet_only, "box.lan"), "https://box.ts.net:8443/")
        self.assertEqual(db.other_where(pantry), "ports 8100, 8200 · pantry · tailnet :18100")
        self.assertEqual(db.other_where(tailnet_only), "port 5173 · tailnet :8443")

    def test_render(self):
        others = [{"name": "<Pantry>", "ports": [8200], "lan": {"8200": ""}, "https": [],
                   "id": "aaa", "icon": True},
                  {"name": "Plain", "ports": [9000], "lan": {"9000": ""}, "https": [],
                   "id": "bbb", "icon": False}]
        page = db.render({**PageTest.STATUS, "others": others}, "box.lan")
        self.assertIn("<h2>Other services</h2>", page)
        self.assertIn('href="http://box.lan:8200/"', page)
        self.assertIn("&lt;Pantry&gt;", page)
        self.assertIn('src="/icon/other-aaa"', page)
        self.assertNotIn("/icon/other-bbb", page)
        self.assertEqual(page.count('class="badge other"'), 1)  # our globe for the one without
        self.assertNotIn("Other services", db.render(PageTest.STATUS, "box.lan"))

    def test_status_looks_in_the_background(self):
        status = db.Status(Path("/nonexistent/config.ini"), Path("/nonexistent"))
        calls = []
        release = threading.Event()

        def slow(skip):
            calls.append(skip)
            release.wait(5)
            return [{"name": f"look {len(calls)}", "id": "aaa", "picture": (b"<svg/>", "image/svg+xml")}]

        with mock.patch.object(db, "other_services", slow), mock.patch.object(db, "OTHERS_WAIT", 0.05):
            self.assertEqual(STATUS_OTHERS(status, {"seerr": {}}), [])  # the first look is slow
            self.assertEqual(calls, [{5055}])
            release.set()
            status.others_job.result()
            self.assertEqual(STATUS_OTHERS(status, {}), [{"name": "look 1", "id": "aaa", "icon": True}])
            self.assertEqual(status.other_icons, {"other-aaa": (b"<svg/>", "image/svg+xml")})
            self.assertEqual(len(calls), 1)  # not again before OTHERS_EVERY
            status.others_at -= db.OTHERS_EVERY + 1
            STATUS_OTHERS(status, {})
            status.others_job.result()
            self.assertEqual(STATUS_OTHERS(status, {})[0]["name"], "look 2")
        status.pool.shutdown()


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
        self.assertIn('href="/open/transmission"', page)
        self.assertIn('href="/open/prowlarr"', page)
        self.assertNotIn(":8096/", page)  # no Jellyfin check, no tile
        self.assertIn("1 thing needs a look", page)
        self.assertIn("&lt;b&gt;nCore&lt;/b&gt;", page)  # escaped
        self.assertIn('class="alert own"', page)  # a tile's problems: a red sign with a tooltip
        self.assertNotIn('class="alert upstream"', page)
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
                    self.assertIn('href="/open/transmission"', resp.read().decode())
                with urllib.request.urlopen(base + "/open/prowlarr") as resp:
                    page = resp.read().decode()
                self.assertIn('<iframe src="http://127.0.0.1:9696/"', page)
                for path in ("/open/sonarr", "/open/nope"):  # no tile for it
                    with self.assertRaises(urllib.error.HTTPError) as e:
                        urllib.request.urlopen(base + path)
                    e.exception.close()
                    self.assertEqual(e.exception.code, 404)
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

    def test_groups(self):
        checks = {"jellyfin": db.result(db.OK, "healthy", version="12.4.0"),
                  "seerr": db.result(db.OK, "running, version 3.5.2", version="3.5.2"),
                  "desireerr": db.result(db.OK, "running, version 1.3", version="1.3"),
                  "transmission": db.result(db.OK, "0 downloading, 54 seeding", version="4.1.1 (56442e2929)",
                                            rate_down=0, rate_up=0),
                  "sonarr": db.result(db.OK, "healthy", version="4.0.20.3014"),
                  "prowlarr": db.result(db.OK, "healthy", version="3.0.2.5780")}
        page = db.render({**self.STATUS, "checks": checks}, "box")
        labels = [page.index(f"<h3>{label}</h3>") for label in ("Watch", "Request", "Download")]
        self.assertEqual(labels, sorted(labels))
        self.assertIn('class="grp g-request wide" style="--n:2"', page)  # its line spans both tiles
        self.assertIn('--cols:4', page)
        self.assertIn(">v12.4.0</div>", page)                            # the dot says it's fine
        self.assertIn('title="0 downloading, 54 seeding · version 4.1.1 (56442e2929)">idle</div>', page)
        self.assertIn('title="healthy · version 3.0.2.5780">v3.0.2</div>', page)
        folded = page[page.index('<details class="folded" data-fold="folded">'):]
        self.assertIn("all 2 fine", folded)
        self.assertLess(page.index("Prowlarr"), page.index("<h2>Health</h2>"))
        self.assertNotIn("<h3>Monitor", page[:page.index('<details class="folded" data-fold="folded">')])
        # a problem in the folded group shows on its closed line
        checks["prowlarr"] = db.result(db.WARN, "1 issue", ["Indexer nCore is unavailable"], version="3.0.2")
        page = db.render({**self.STATUS, "checks": checks}, "box")
        summary = page[page.index('<details class="folded" data-fold="folded">'):page.index("</summary>")]
        self.assertIn("<b>Prowlarr</b>: 1 issue", summary)
        self.assertNotIn("all 2 fine", summary)
        self.assertIn('<i class="dot warn">', summary)

    def test_open_page_bar(self):
        checks = {cid: db.result(db.OK, "fine") for cid in ("jellyfin", "seerr", "transmission",
                                                            "sonarr", "prowlarr")}
        checks["prowlarr"] = db.result(db.DOWN, "not responding")
        page = db.render_open({**self.STATUS, "checks": checks}, "jellyfin", "box")
        nav = page[page.index("<nav>"):page.index("</nav>")]
        self.assertLess(nav.index("<small>Watch</small>"), nav.index("<small>Request</small>"))
        self.assertIn('<span class="fold">', nav)                        # the folded group, as a menu
        self.assertIn('<i class="dot down"></i>▾</summary>', nav)       # with what's wrong in it
        page = db.render_open({**self.STATUS, "checks": checks}, "sonarr", "box")
        self.assertIn('<summary class="on"', page)                       # its app is open

    def test_open_page(self):
        status = dict(self.STATUS, logos=["prowlarr"])
        page = db.render_open(status, "prowlarr", "box.lan")
        self.assertIn('<a class="home" href="/"', page)                     # back to the start page
        self.assertIn('<iframe src="http://box.lan:9696/"', page)
        self.assertIn('href="http://box.lan:9696/" target="_blank"', page)  # open on its own
        self.assertIn('<a href="/open/prowlarr" class="on"', page)          # the switcher
        self.assertIn('<a href="/open/transmission"', page)
        self.assertNotIn("/open/samba", page)                               # health rows aren't apps
        self.assertIn('<link rel="icon" href="/icon/prowlarr">', page)
        self.assertIn("<title>Prowlarr · box</title>", page)
        # Transmission only lets its own origin frame it: through the start page
        page = db.render_open(status, "transmission", "box.lan")
        self.assertIn('<iframe src="/transmission/web/"', page)
        self.assertIn('href="http://box.lan:9091/" target="_blank"', page)

    def test_open_page_at(self):
        # where the framed app said it was (?at=) opens again after a reload
        page = db.render_open(self.STATUS, "prowlarr", "box.lan", at="/performer/1?q=a&b=c#x")
        self.assertIn('<iframe src="http://box.lan:9696/performer/1?q=a&amp;b=c#x"', page)
        self.assertIn("frame.contentWindow", page)
        self.assertEqual(db.frame_at("at=%2Fscene%2Fab-12%3Fsort%3Ddate"), "/scene/ab-12?sort=date")
        for bad in ("at=%2F%2Fevil.example%2F", "at=%2F%5Cevil.example", "at=http%3A%2F%2Fx%2F",
                    "at=scene", "at=%2Fa%20b", "at=%2Fa%0Ab", ""):
            self.assertEqual(db.frame_at(bad), "", bad)

    def test_transmission_passed_through(self):
        with tempfile.TemporaryDirectory() as tmp, FakeServer() as tr:
            status = db.Status(Path(tmp) / "config.ini", Path(tmp))
            db.Handler.status = status
            httpd = db.Server(("127.0.0.1", 0), db.Handler)
            threading.Thread(target=httpd.serve_forever, args=(0.01,), daemon=True).start()
            self.addCleanup(httpd.server_close)
            self.addCleanup(httpd.shutdown)
            with mock.patch.object(db, "TRANSMISSION", ("127.0.0.1", tr.httpd.server_address[1])):
                client = db.Transmission(f"http://127.0.0.1:{httpd.server_address[1]}/transmission/rpc",
                                         "user", "pass")
                tr.tr_torrents = [{"name": "x"}]
                self.assertEqual(client.call("torrent-get", {"fields": ["name"]}),
                                 {"torrents": [{"name": "x"}]})  # the 409 dance, the login
                self.assertEqual(tr.requests[-1], ("POST", "/transmission/rpc"))
                with self.assertRaises(urllib.error.HTTPError) as e:
                    db.Transmission(client.url, "user", "wrong").call("torrent-get", {})
                e.exception.close()
                self.assertEqual(e.exception.code, 401)
            with mock.patch.object(db, "TRANSMISSION", ("127.0.0.1", 1)):  # nothing there
                with self.assertRaises(urllib.error.HTTPError) as e:
                    urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_address[1]}/transmission/web/")
                e.exception.close()
                self.assertEqual(e.exception.code, 502)

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
                    mock.patch.object(db, "check_desireerr", lambda s, arr_dir: db.result(db.OK, "x")), \
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
                r = db.check_desireerr({}, "/nonexistent")  # not this box's .env
            self.assertEqual((r["state"], r["summary"]), (state, summary))
        with mock.patch.object(db, "http_get", side_effect=ConnectionRefusedError(111, "refused")):
            self.assertEqual(db.check_desireerr({}, "/nonexistent")["state"], db.DOWN)

    def test_desireerr_check_says_which_build(self):
        with tempfile.TemporaryDirectory() as arr, \
                mock.patch.object(db, "http_get", lambda url: b'{"setup_needed": false, "version": "1.3"}'):
            env = Path(arr, ".env")
            self.assertEqual(db.check_desireerr({}, arr)["build"], "")  # no .env: the release
            for text, build in (("DESIREERR_BUILD=local:26c5293+\n", "local 26c5293+"),
                                ("DESIREERR_CODE=/home/me/desireerr\n", "dev"),
                                ("DESIREERR_IMAGE=ghcr.io/vibesclauder/desireerr:dev\n", "edge"),
                                ("OTHER=1\n", "")):
                with self.subTest(env=text):
                    env.write_text(text)
                    c = db.check_desireerr({}, arr)
                    self.assertEqual((c["state"], c["build"]), (db.OK, build))
                    self.assertEqual(db.tile_line(c), "v1.3" + (f" · {build}" if build else ""))
            with mock.patch.object(db, "http_get", lambda url: b'{"setup_needed": true}'):
                c = db.check_desireerr({}, arr)
            self.assertIn(f"the setup code is in {arr}/desireerr/data/setup-code", c["details"])

    def test_seerr_check(self):
        appdata = b'{"appData": true, "appDataPath": "/app/config", "appDataPermissions": true}'
        def seerr(github_hangs=False, writable=True):
            def get(url, timeout=db.TIMEOUT):
                if url.endswith("/appdata"):
                    return appdata if writable else appdata.replace(b"s\": true", b"s\": false")
                if github_hangs:  # /status waits on GitHub for its update check
                    raise urllib.error.URLError(TimeoutError("timed out"))
                return b'{"version": "3.5.1"}'
            return get
        with mock.patch.object(db, "seerr_version", None):
            with mock.patch.object(db, "http_get", seerr(github_hangs=True)):
                r = db.check_seerr({})
            self.assertEqual((r["state"], r["summary"]), (db.OK, "running, version ?"))
            self.assertIn("GitHub", r["details"][0])
            with mock.patch.object(db, "http_get", seerr()):
                r = db.check_seerr({})
            self.assertEqual((r["state"], r["summary"], r["details"]),
                             (db.OK, "running, version 3.5.1", []))
            with mock.patch.object(db, "http_get", seerr(github_hangs=True)):
                self.assertEqual(db.check_seerr({})["summary"], "running, version 3.5.1")
            with mock.patch.object(db, "http_get", seerr(writable=False)):
                self.assertEqual(db.check_seerr({})["state"], db.WARN)
        with mock.patch.object(db, "http_get", side_effect=urllib.error.URLError(TimeoutError())):
            r = db.check_seerr({})
        self.assertEqual((r["state"], r["summary"]), (db.DOWN, "not responding"))

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


def fake_github(ahead, version="1.1 rev 2"):
    """GitHub's API for check_update: BRANCH is `ahead` commits past the installed one, and its
    VERSION says `version`."""
    commits = [{"sha": f"sha{i}", "commit": {"message": f"Change {i}\n\nbody",
                                             "committer": {"date": "2026-11-20T09:30:00Z"}}}
               for i in range(1, ahead + 1)]

    def fetch(path):
        if path.startswith("compare/installed..."):
            return {"ahead_by": ahead, "commits": commits,
                    "html_url": "https://github.com/x/compare/installed...main"}
        if path == f"contents/VERSION?ref=sha{ahead}":
            return {"content": base64.b64encode(version.encode() + b"\n").decode()}
        raise AssertionError(path)
    return fetch


class UpdateTest(unittest.TestCase):
    def test_check(self):
        self.assertIsNone(db.check_update("", fake_github(3)))  # not installed from git
        self.assertEqual(db.check_update("installed", fake_github(0)), {"commits": 0})
        info = db.check_update("installed", fake_github(3))
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(1795167000))
        self.assertEqual(info["version"], f"1.1 rev 2, {when}")
        self.assertEqual((info["commits"], info["head"]), (3, "sha3"))
        self.assertEqual(info["subjects"], ["Change 3", "Change 2", "Change 1"])  # newest first
        self.assertEqual(info["url"], "https://github.com/x/compare/installed...main")
        many = db.check_update("installed", fake_github(9))
        self.assertEqual(len(many["subjects"]), db.UPDATE_SUBJECTS)
        odd = db.check_update("installed", fake_github(1, version="2.0"))
        self.assertTrue(odd["version"].startswith("2.0, "))

    def test_command_names_the_checkout(self):
        with mock.patch.object(db, "installed", lambda: {"repo": "/home/me/torrent-box"}), \
                mock.patch("os.path.expanduser", lambda p: "/home/me"):
            self.assertEqual(db.update_command(), "cd ~/torrent-box && git pull && ./setup.sh")
        with mock.patch.object(db, "installed", lambda: {"repo": "/opt/tb"}):
            self.assertEqual(db.update_command(), "cd /opt/tb && git pull && ./setup.sh")
        with mock.patch.object(db, "INSTALLED_FILE", Path("/nonexistent/installed.json")):
            self.assertEqual(db.update_command(), "cd torrent-box && git pull && ./setup.sh")
            self.assertIsNone(db.check_update(fetch=fake_github(3)))  # no commit: no checks

    def test_push_once_per_head_at_most_daily(self):
        with FakeServer() as server, tempfile.TemporaryDirectory() as tmp:
            s = db.load_settings(write_config(Path(tmp), server.base_url))
            state = Path(tmp) / "state"
            state.mkdir()
            ahead = [2]
            check = lambda: db.check_update("installed", fake_github(ahead[0]))  # noqa: E731
            u = db.Updates(state, check)
            with mock.patch("sys.stderr"), mock.patch.object(db, "version_text", lambda: "1.1 rev 0, x"):
                u.poll(s, 1000)
                self.assertEqual(len(server.pushes), 1)
                push = server.pushes[0]
                self.assertTrue(push["title"].startswith("torrent-box 1.1 rev 2, "))
                self.assertIn("2 new commits (you have 1.1 rev 0, x):\n• Change 2\n• Change 1",
                              push["message"])
                self.assertIn("git pull && ./setup.sh", push["message"])
                self.assertEqual(push["click"], "https://github.com/x/compare/installed...main")
                u.poll(s, 1060)  # not due yet
                u.poll(s, 1000 + db.UPDATE_EVERY)  # due, same head
                self.assertEqual(len(server.pushes), 1)
                ahead[0] = 3  # a new commit, but the last push is less than a day old
                u.poll(s, 1000 + 2 * db.UPDATE_EVERY)
                self.assertEqual(len(server.pushes), 1)
                self.assertEqual(u.info["commits"], 3)  # the page has it already
                u.poll(s, 1000 + 4 * db.UPDATE_EVERY)
                self.assertEqual(len(server.pushes), 2)
                # a restart remembers what was pushed
                again = db.Updates(state, check)
                again.poll(s, 1000 + 9 * db.UPDATE_EVERY)
                self.assertEqual(len(server.pushes), 2)

    def test_failures(self):
        with FakeServer() as server, tempfile.TemporaryDirectory() as tmp:
            s = db.load_settings(write_config(Path(tmp), server.base_url))

            def offline():
                raise urllib.error.URLError("no network")

            u = db.Updates(None, offline)
            with mock.patch("sys.stderr"):
                u.poll(s, 0)
            self.assertEqual((u.info, u.due), (None, db.UPDATE_RETRY))
            u.check = lambda: db.check_update("installed", fake_github(1))
            server.ntfy_down = True
            with mock.patch("sys.stderr"):
                u.poll(s, db.UPDATE_RETRY)
            self.assertEqual(u.info["commits"], 1)  # shown, but the push is tried again
            server.ntfy_down = False
            with mock.patch("sys.stderr"):
                u.poll(s, db.UPDATE_RETRY + db.UPDATE_EVERY)
            self.assertEqual(len(server.pushes), 1)

    def test_banner(self):
        status = {"time": 0, "hostname": "box", "storage_root": "/mnt/storage",
                  "checks": {"transmission": db.result(db.OK, "fine")}}
        for update in (None, {"commits": 0}):
            self.assertNotIn('class="banner', db.render({**status, "update": update}, "box"))
        update = {"commits": 7, "head": "h", "version": "1.1 rev 7, 2026-11-20 10:30",
                  "subjects": ["Fix <b>this</b>", "Add that"], "url": "https://github.com/x/c"}
        page = db.render({**status, "update": update}, "box")
        self.assertIn('class="banner update"', page)
        self.assertIn("Update available: torrent-box 1.1 rev 7, 2026-11-20 10:30", page)
        self.assertIn("<li>Fix &lt;b&gt;this&lt;/b&gt;</li>", page)
        self.assertIn("<li>… and 5 more</li>", page)
        self.assertIn("git pull &amp;&amp; ./setup.sh", page)
        self.assertIn("All systems go", page)  # an update isn't a problem
