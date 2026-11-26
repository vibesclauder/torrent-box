"""[setup] ups: rss2transmission --ups-event (pushes, the grid log, pausing Transmission),
setup_helper's UPS detection and NUT config, the notify hook and sudoers rule, setup.sh's
choose_ups / setup_ups / remove_ups (sourced, in dry runs) and the start page's UPS row."""

import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from helpers import FakeServer, write_config
import dashboard as db
import rss2transmission as r2t
import setup_helper as sh

REPO = Path(__file__).resolve().parents[1]
UTC = timezone.utc
T0 = datetime(2026, 11, 26, 20, 0, 0, tzinfo=UTC)
ON_MAINS = {"ups.status": "OL", "battery.charge": "100", "battery.runtime": "2460",
            "ups.load": "12", "device.mfr": "EATON", "device.model": "Ellipse PRO 850"}
ON_BATTERY = {**ON_MAINS, "ups.status": "OB DISCHRG", "battery.charge": "87",
              "battery.runtime": "1380"}
# The start of an Eaton Ellipse PRO's HID report descriptor, and a keyboard's
EATON_DESCRIPTOR = bytes.fromhex("05840904a101850109240b240084") + bytes(20)
KEYBOARD_DESCRIPTOR = bytes.fromhex("05010906a101050719e029e7")


def torrent(name, status):
    return {"hashString": name, "name": name, "percentDone": 0.5, "downloadDir": "/x",
            "sizeWhenDone": 1, "status": status}


class UpsEventTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.tmp = Path(self.tmpdir.name)
        self.server = FakeServer().__enter__()
        self.addCleanup(self.server.__exit__)
        self.config = write_config(self.tmp, self.server.base_url)
        self.cfg = r2t.load_config(self.config)
        self.state = self.tmp / "state"
        self.grid = self.state / r2t.GRID_LOG
        self.paused = self.state / r2t.UPS_PAUSED
        self.server.tr_torrents = [torrent("aa", 4), torrent("bb", 6), torrent("cc", 0)]

    def event(self, name, when, ups=ON_MAINS, **cfg):
        for k, v in cfg.items():
            setattr(self.cfg, k, v)
        tr = r2t.Transmission(self.cfg.rpc_url, self.cfg.rpc_user, self.cfg.rpc_password, 5)
        ntfy = r2t.Ntfy(self.cfg.ntfy_server, self.cfg.ntfy_topic, 5)
        return r2t.ups_event(self.cfg, tr, ntfy, name, self.state, lambda: ups, when)

    def calls(self, method):
        return [args for m, args in self.server.rpc_calls if m == method]

    def grid_lines(self):
        return [l.split("\t")[1:] for l in self.grid.read_text().splitlines() if not l.startswith("#")]

    def test_power_cut_and_back(self):
        self.assertEqual(self.event("ONBATT", T0, ON_BATTERY), 0)
        push = self.server.pushes[-1]
        self.assertIn("Power cut", push["title"])
        self.assertEqual(push["priority"], 4)
        self.assertIn("battery 87%, about 23 min left, load 12%", push["message"])
        self.assertIn("2 torrents paused", push["message"])
        self.assertEqual(self.calls("torrent-stop"), [{"ids": ["aa", "bb"]}])  # cc was stopped already
        self.assertEqual(json.loads(self.paused.read_text()), ["aa", "bb"])
        self.assertEqual(self.grid_lines(), [["out"]])
        self.assertTrue(self.grid.read_text().startswith("# torrent-box"))

        # a second ONBATT (upsmon restarted during the cut): no second "out", the list is kept
        self.event("ONBATT", T0 + timedelta(minutes=1), ON_BATTERY)
        self.assertEqual(self.grid_lines(), [["out"]])
        self.assertEqual(json.loads(self.paused.read_text()), ["aa", "bb"])

        self.assertEqual(self.event("ONLINE", T0 + timedelta(minutes=4, seconds=12)), 0)
        push = self.server.pushes[-1]
        self.assertEqual(push["title"].split(" on ")[0], "Power is back")
        self.assertTrue(push["title"].endswith("after 4 min 12 s"))
        self.assertEqual(push["priority"], 3)
        self.assertIn("2 torrents resumed", push["message"])
        self.assertEqual(self.calls("torrent-start"), [{"ids": ["aa", "bb"]}])  # not cc
        self.assertFalse(self.paused.exists())
        self.assertEqual(self.grid_lines(), [["out"], ["back", "4 min 12 s"]])

    def test_online_without_a_cut_pushes_nothing(self):
        self.assertEqual(self.event("ONLINE", T0), 0)
        self.assertEqual(self.server.pushes, [])
        self.assertFalse(self.grid.exists())

    def test_no_pausing(self):
        self.event("ONBATT", T0, ON_BATTERY, pause_on_battery=False)
        self.assertEqual(self.calls("torrent-stop"), [])
        self.assertNotIn("Transmission", self.server.pushes[-1]["message"])
        self.assertEqual(self.grid_lines(), [["out"]])

    def test_transmission_down(self):
        self.server.rpc_down = True
        self.assertEqual(self.event("ONBATT", T0, ON_BATTERY), 0)  # the push matters more
        self.assertIn("couldn't be paused", self.server.pushes[-1]["message"])

    def test_push_failure_is_an_error(self):
        self.server.ntfy_down = True
        self.assertEqual(self.event("LOWBATT", T0, ON_BATTERY), 1)

    def test_low_battery_and_shutdown(self):
        self.event("ONBATT", T0, ON_BATTERY)
        self.event("LOWBATT", T0 + timedelta(minutes=30), ON_BATTERY)
        self.assertEqual(self.server.pushes[-1]["priority"], 5)
        self.event("SHUTDOWN", T0 + timedelta(minutes=30, seconds=5), ON_BATTERY)
        self.assertEqual(self.server.pushes[-1]["priority"], 5)
        self.assertIn("shutting down", self.server.pushes[-1]["title"])
        self.assertEqual(self.grid_lines(), [["out"], ["shutdown"]])

        # the box boots when the power is back: the next timer run logs it
        boot = T0 + timedelta(hours=2)
        self.assertIsNone(r2t.grid_after_boot(self.grid, lambda: ON_BATTERY, lambda: boot))  # not yet
        self.assertIsNone(r2t.grid_after_boot(self.grid, lambda: ON_MAINS,
                                              lambda: T0 - timedelta(days=1)))  # booted before the cut
        self.assertEqual(r2t.grid_after_boot(self.grid, lambda: ON_MAINS, lambda: boot), 7200)
        self.assertEqual(self.grid_lines()[-1][:2], ["back", "2 h"])
        self.assertIsNone(r2t.grid_after_boot(self.grid, lambda: ON_MAINS, lambda: boot))  # once
        self.assertEqual(len(self.grid_lines()), 3)

    def test_other_events(self):
        for name, priority in (("COMMBAD", 4), ("NOCOMM", 4), ("COMMOK", 3), ("REPLBATT", 3)):
            self.event(name, T0)
            self.assertEqual(self.server.pushes[-1]["priority"], priority, name)
        self.assertFalse(self.grid.exists())

    def test_main(self):
        with mock.patch.object(r2t, "upsc", return_value=ON_BATTERY):
            code = r2t.main(["-c", str(self.config), "--state", str(self.state / "state.sqlite"),
                             "--ups-event", "ONBATT"])
        self.assertEqual(code, 0)
        self.assertIn("Power cut", self.server.pushes[-1]["title"])
        self.assertTrue(self.paused.exists())
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            r2t.main(["-c", str(self.config), "--ups-event", "FSD"])  # not one we handle

    def test_new_torrents_wait_while_on_battery(self):
        tr = r2t.Transmission(self.cfg.rpc_url, self.cfg.rpc_user, self.cfg.rpc_password, 5)
        tr.add(b"d4:infod4:name1:aee", "/x")
        tr.add_paused = True
        tr.add(b"d4:infod4:name1:bee", "/x")
        adds = self.calls("torrent-add")
        self.assertNotIn("paused", adds[0])
        self.assertTrue(adds[1]["paused"])

    def test_config(self):
        self.assertTrue(self.cfg.pause_on_battery)
        example = (REPO / "config.example.ini").read_text()
        self.assertRegex(example, r"\npause_on_battery = yes\n")
        self.assertRegex(example, r"\nups +=\n")
        self.assertRegex(example, r"\nups_low_battery =\n")

    def test_durations(self):
        self.assertEqual(r2t.duration(42), "42 s")
        self.assertEqual(r2t.duration(252), "4 min 12 s")
        self.assertEqual(r2t.duration(1380), "23 min")
        self.assertEqual(r2t.duration(5400), "1 h 30 min")


class EventListsTest(unittest.TestCase):
    """The events are listed in rss2transmission, setup.sh (sudoers), ups-notify and upsmon.conf:
    the same everywhere, or an event gets lost (or refused by sudo)."""

    def test_same_everywhere(self):
        events = set(r2t.UPS_EVENTS)
        self.assertEqual(set(r2t.UPS_PUSHES), events)
        setup = (REPO / "setup.sh").read_text()
        self.assertEqual(set(re.search(r"^UPS_EVENTS=\(([^)]*)\)", setup, re.M)[1].split()), events)
        notify = (REPO / "templates/ups-notify").read_text()
        self.assertEqual(set(re.search(r"^\s+([A-Z|]+)\) ;;", notify, re.M)[1].split("|")), events)
        upsmon = (REPO / "templates/nut-upsmon.conf").read_text()
        self.assertEqual(set(re.findall(r"^NOTIFYFLAG (\w+) SYSLOG\+EXEC", upsmon, re.M)), events)
        self.assertIn(f'UPS_NAME={r2t.UPS.split("@")[0]} ', setup)
        self.assertIn(f"\nUPS_LOW_DEFAULT=40 ", setup)
        self.assertIn(f"NUT_FILES=({' '.join(sh.NUT_FILES)})", setup)
        self.assertIn(f"UPS_MARKER={db.UPS_FILE} ", setup)


class DetectTest(unittest.TestCase):
    def test_usage_page(self):
        self.assertEqual(sh.first_usage_page(EATON_DESCRIPTOR), 0x84)
        self.assertEqual(sh.first_usage_page(KEYBOARD_DESCRIPTOR), 0x01)
        self.assertEqual(sh.first_usage_page(bytes.fromhex("0685000901")), 0x85)  # 2-byte page
        self.assertIsNone(sh.first_usage_page(b""))

    def test_sysfs(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            hid = tmp / "bus/hid/devices"
            hid.mkdir(parents=True)
            for usb, name, descriptor, maker, product in (
                    ("1-4", "0003:046D:C52B.0001", KEYBOARD_DESCRIPTOR, "Logitech", "Receiver"),
                    ("1-5", "0003:0463:FFFF.0004", EATON_DESCRIPTOR, "EATON", "Ellipse PRO")):
                dev = tmp / "devices/usb1" / usb / f"{usb}:1.0" / name
                dev.mkdir(parents=True)
                (dev / "report_descriptor").write_bytes(descriptor)
                (dev.parent.parent / "manufacturer").write_text(maker + "\n")
                (dev.parent.parent / "product").write_text(product + "\n")
                (hid / name).symlink_to(dev)
            self.assertEqual(sh.ups_devices(hid), [("0463:ffff", "EATON Ellipse PRO")])
            self.assertEqual(sh.ups_devices(tmp / "nothing"), [])

            # NUT's driver holds the UPS: no HID device, its interface bound to usbfs
            usb = tmp / "bus/usb/devices"
            usb.mkdir(parents=True)
            drivers = tmp / "bus/usb/drivers"
            for name in ("usbfs", "usbhid"):
                (drivers / name).mkdir(parents=True)
            for dev, driver in (("1-4", "usbhid"), ("1-5", "usbfs")):
                iface = tmp / "devices/usb1" / dev / f"{dev}:1.0"
                (iface / "bInterfaceClass").write_text("03\n")
                (iface / "driver").symlink_to(drivers / driver)
                (iface.parent / "idVendor").write_text("0463\n" if dev == "1-5" else "046d\n")
                (iface.parent / "idProduct").write_text("ffff\n" if dev == "1-5" else "c52b\n")
                (usb / f"{dev}:1.0").symlink_to(iface)
            self.assertEqual(sh.claimed_hid_devices(usb), [("0463:ffff", "EATON Ellipse PRO")])


class NutConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.out = Path(self.tmpdir.name)

    def files(self):
        return {p.name: p.read_text() for p in self.out.iterdir()}

    def test_files(self):
        sh.nut_config(self.out, 40, "0463:ffff", 'EATON "Ellipse" PRO', "/usr/local/lib/torrent-box")
        f = self.files()
        self.assertEqual(set(f), set(sh.NUT_FILES))
        self.assertTrue(all(oct(p.stat().st_mode)[-3:] == "600" for p in self.out.iterdir()))
        self.assertIn("MODE=standalone", f["nut.conf"])
        self.assertIn("\tvendorid = 0463\n\tproductid = ffff\n", f["ups.conf"])
        self.assertIn('desc = "EATON Ellipse PRO"', f["ups.conf"])
        self.assertIn("override.battery.charge.low = 40", f["ups.conf"])
        self.assertIn("\tignorelb\n", f["ups.conf"])
        self.assertIn("LISTEN 127.0.0.1 3493", f["upsd.conf"])
        self.assertIn("NOTIFYCMD /usr/local/lib/torrent-box/ups-notify", f["upsmon.conf"])
        password = sh.upsd_password(f["upsd.users"])
        self.assertGreaterEqual(len(password), 20)
        self.assertIn(f"MONITOR ups@127.0.0.1 1 upsmon {password} primary", f["upsmon.conf"])
        self.assertIn('SHUTDOWNCMD "/sbin/shutdown -h +0"', f["upsmon.conf"])
        for name in sh.NUT_FILES.values():  # no password in the repo
            self.assertNotIn(password, (REPO / "templates" / name).read_text())

    def test_password_kept_and_no_device(self):
        sh.nut_config(self.out, 55, "", "", "/lib", "# ours\n[upsmon]\n\tpassword = s3cret\n\tupsmon primary\n")
        f = self.files()
        self.assertEqual(sh.upsd_password(f["upsd.users"]), "s3cret")
        self.assertNotIn("vendorid", f["ups.conf"])
        self.assertIn('desc = "UPS"', f["ups.conf"])
        self.assertIn("override.battery.charge.low = 55", f["ups.conf"])
        sh.nut_config(self.out, 40, "", "", "/lib", "garbage")
        self.assertNotEqual(sh.upsd_password(self.files()["upsd.users"]), "s3cret")  # a new one

    def test_cli(self):
        result = subprocess.run(["python3", str(REPO / "setup_helper.py"), "nut-config", str(self.out),
                                 "--match", "0463:ffff", "--desc", "EATON Ellipse PRO"],
                                input="", capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")  # nothing printed: the password stays in the files
        self.assertEqual(set(self.files()), set(sh.NUT_FILES))


class NotifyHookTest(unittest.TestCase):
    """templates/ups-notify (as upsmon runs it) and templates/ups-sudoers."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.tmp = Path(self.tmpdir.name)
        values = {"USER": "alice", "BIN": "/home/alice/.local/bin/rss2transmission",
                  "CONFIG": "/home/alice/.config/rss2transmission/config.ini"}
        self.hook = self.tmp / "ups-notify"
        self.hook.write_text(sh.render((REPO / "templates/ups-notify").read_text(), values))
        (self.tmp / "sudo").write_text('#!/bin/sh\nprintf "%s\\n" "$*" >>"$CALLS"\n')
        (self.tmp / "sudo").chmod(0o755)
        self.commands = [f"{values['BIN']} -c {values['CONFIG']} --ups-event {e}" for e in r2t.UPS_EVENTS]
        self.sudoers = sh.render((REPO / "templates/ups-sudoers").read_text(),
                                 {"USER": "alice", "COMMANDS": ", ".join(self.commands)})

    def notify(self, event):
        calls = self.tmp / "calls"
        calls.write_text("")
        subprocess.run(["sh", str(self.hook), f"UPS ups on battery"], check=True,
                       env={"PATH": f"{self.tmp}:/usr/bin:/bin", "CALLS": str(calls),
                            "NOTIFYTYPE": event, "UPSNAME": "ups@127.0.0.1"})
        return calls.read_text().strip()

    def test_hook(self):
        self.assertEqual(self.notify("ONBATT"), "-n -u alice " + self.commands[1])
        self.assertEqual(self.notify("FSD"), "")  # not one of ours
        self.assertEqual(self.notify("ONBATT; rm -rf /"), "")

    def test_hook_runs_exactly_the_allowed_commands(self):
        for event in r2t.UPS_EVENTS:
            called = self.notify(event).removeprefix("-n -u alice ")
            self.assertIn(called, self.commands)
            self.assertIn(called, self.sudoers)

    @unittest.skipUnless(shutil.which("visudo"), "no visudo")
    def test_sudoers_parses(self):
        path = self.tmp / "sudoers"
        path.write_text(self.sudoers)
        result = subprocess.run(["visudo", "-cqf", str(path)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("nut ALL=(alice) NOPASSWD: TORRENT_BOX_UPS", self.sudoers)


class SetupShTest(unittest.TestCase):
    """setup.sh sourced, its UPS steps driven in dry runs against a temp dir."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.tmp = Path(self.tmpdir.name)
        self.config = self.tmp / "config.ini"
        self.config.write_text("[setup]\nups =\nups_low_battery =\n")

    def bash(self, body, detect="0463:ffff\tEATON Ellipse PRO", marker=False, ups="", low=""):
        self.config.write_text(f"[setup]\nups = {ups}\nups_low_battery = {low}\n")
        if marker:
            (self.tmp / "ups.json").write_text("{}")
        script = f"""
set -euo pipefail
source {REPO}/setup.sh
DRY_RUN=1 TMP={self.tmp}/work config_src={self.config} CONFIG={self.config}
mkdir -p "$TMP"
NUT_DIR={self.tmp}/etc-nut UPS_MARKER={self.tmp}/ups.json UPS_SUDOERS={self.tmp}/sudoers
LIB={self.tmp}/lib STATE_DIR={self.tmp}/state BIN=/home/alice/.local/bin/rss2transmission
real_helper() {{ PYTHONDONTWRITEBYTECODE=1 python3 "$REPO/setup_helper.py" "$@"; }}
helper() {{ if [[ $1 == ups-detect ]]; then printf '%s' "$DETECT"; else real_helper "$@"; fi; }}
sudo() {{ return 1; }}
systemctl() {{ return 1; }}
apt_install() {{ info "would install: $*"; }}
{body}
"""
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                              env={**os.environ, "DETECT": detect + ("\n" if detect else "")})

    def choose(self, **kw):
        result = self.bash('choose_ups; echo "$ups $ups_on $ups_low [$ups_device] [$ups_model]"', **kw)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip().splitlines()[-1]

    def test_choose(self):
        self.assertEqual(self.choose(), "auto yes 40 [0463:ffff] [EATON Ellipse PRO]")
        self.assertEqual(self.choose(detect=""), "auto no 40 [] []")  # boxes without one: nothing
        self.assertEqual(self.choose(detect="", marker=True), "auto yes 40 [] []")  # unplugged now
        # NUT's driver holds it (no longer a HID device): the one in the marker
        (self.tmp / "ups.json").write_text('{"device": "0463:ffff", "model": "EATON Ellipse PRO"}')
        result = self.bash('choose_ups; echo "$ups_on [$ups_device] [$ups_model] $ups_seen"', detect="")
        self.assertEqual(result.stdout.strip().splitlines()[-1], "yes [0463:ffff] [EATON Ellipse PRO] 1")
        (self.tmp / "ups.json").unlink()
        self.assertEqual(self.choose(ups="no"), "no no 40 [0463:ffff] [EATON Ellipse PRO]")
        self.assertEqual(self.choose(ups="yes", detect="", low="30"), "yes yes 30 [] []")
        for bad in ({"ups": "maybe"}, {"low": "5"}, {"low": "40%"}):
            result = self.bash("choose_ups", **bad)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("ERROR", result.stderr)

    def test_setup_dry_run(self):
        result = self.bash("choose_ups; setup_ups; show_changes")
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout
        self.assertIn("found on USB: EATON Ellipse PRO (0463:ffff)", out)
        self.assertIn("would install: nut-client nut-server", out)
        self.assertIn("would run: sudo udevadm trigger --action=add --subsystem-match=usb "
                      "--attr-match=idVendor=0463 --attr-match=idProduct=ffff", out)
        for f in sh.NUT_FILES:
            self.assertIn(f"would create {self.tmp}/etc-nut/{f} (mode 640)", out)
        self.assertIn(f"would create {self.tmp}/lib/ups-notify", out)
        self.assertIn(f"would create {self.tmp}/sudoers (mode 440)", out)
        self.assertIn("would run: sudo systemctl enable nut-monitor.service", out)
        self.assertIn("would run: sudo systemctl restart nut-driver@ups.service nut-server.service "
                      "nut-monitor.service", out)
        sudoers = (self.tmp / "work/ups-sudoers").read_text()
        self.assertIn("/home/alice/.local/bin/rss2transmission -c", sudoers)
        self.assertEqual(json.loads((self.tmp / "work/ups.json").read_text()),
                         {"name": "ups", "low_battery": 40, "grid_log": f"{self.tmp}/state/grid.log",
                          "device": "0463:ffff", "model": "EATON Ellipse PRO"})

    def test_off(self):
        result = self.bash("choose_ups; setup_ups", detect="")
        self.assertIn("no UPS on USB", result.stdout)
        self.assertNotIn("would", result.stdout)
        result = self.bash("choose_ups; setup_ups", ups="no", marker=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("NUT is stopped", result.stdout)
        self.assertIn("would put back the package's", result.stdout)
        self.assertIn(f"would run: sudo rm -f {self.tmp}/sudoers {self.tmp}/lib/ups-notify "
                      f"/etc/killpower {self.tmp}/ups.json", result.stdout)


class DashboardTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.tmp = Path(self.tmpdir.name)
        self.grid = self.tmp / "grid.log"
        self.info = self.tmp / "ups.json"
        self.info.write_text(json.dumps({"name": "ups", "low_battery": 40, "grid_log": str(self.grid)}))

    def check(self, ups, now=None):
        return db.check_ups({}, now, self.info, lambda name: ups if name == "ups@127.0.0.1" else {})

    def test_states(self):
        c = self.check(ON_MAINS)
        self.assertEqual((c["state"], c["summary"]), (db.OK, "On mains, battery 100%, 41 min left, load 12%"))
        self.assertEqual(c["metrics"]["model"], "EATON Ellipse PRO 850")
        c = self.check(ON_BATTERY)
        self.assertEqual(c["state"], db.WARN)
        self.assertTrue(c["summary"].startswith("On battery, battery 87%"))
        self.assertIn("shuts down at 40%", c["details"][0])
        self.assertEqual(self.check({**ON_BATTERY, "ups.status": "OB LB"})["state"], db.DOWN)
        self.assertEqual(self.check({**ON_MAINS, "ups.status": "OL CHRG RB"})["summary"].split(",")[0],
                         "On mains (charging")
        self.assertEqual(self.check({**ON_MAINS, "ups.status": "OL RB"})["state"], db.WARN)
        self.assertEqual(self.check({})["state"], db.DOWN)  # driver or upsd down

    def test_grid(self):
        now = datetime(2026, 11, 26, 12, 0, tzinfo=UTC).timestamp()
        self.grid.write_text(r2t.GRID_HEADER + "".join(f"{when}\t{what}\n" for when, what in (
            ("2026-09-01T10:00:00+02:00", "out"), ("2026-09-01T10:05:00+02:00", "back\t5 min"),
            ("2026-11-20T08:00:00+01:00", "out"), ("2026-11-20T08:00:30+01:00", "back\t30 s"),
            ("2026-11-25T20:00:00+01:00", "out"), ("2026-11-25T20:40:00+01:00", "shutdown"),
            ("2026-11-25T23:00:00+01:00", "back\t3 h\tthe box was off"),
            ("2026-11-26T12:55:00+01:00", "out"))))
        outages = db.grid_outages(self.grid, now)
        self.assertEqual([(s, e) for _, s, e in outages],
                         [(None, ""), (3 * 3600, "the box shut down"), (30, "")])  # September's is old
        c = self.check(ON_BATTERY, now)
        html = db.ups_html(c, now)
        self.assertIn("Grid: 3 power cuts in 30 days, 3 h in all", html)
        self.assertIn("still out", html)
        self.assertIn("out 3 h, the box shut down", html)
        self.assertIn("shuts down at 40%", html)
        self.grid.unlink()
        self.assertIn("no power cuts in 30 days", db.ups_html(self.check(ON_MAINS, now), now))

    def test_shown_only_when_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = write_config(Path(tmp), "http://127.0.0.1:1")
            s = db.load_settings(config)
            self.assertTrue(s["ups"])  # auto, when it isn't there
            config.write_text(re.sub(r"(?m)^ups +=.*$", "ups = no", config.read_text()))
            self.assertFalse(db.load_settings(config)["ups"])
        checks = lambda s: set(db.gather(s, self.tmp, pool)["checks"])  # noqa: E731
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(4) as pool, \
                mock.patch.object(db, "service_check", return_value=(db.OK, "running")), \
                mock.patch.object(db, "check_ups", return_value=db.result(db.OK, "On mains")):
            s = {**db.load_settings(Path("/nonexistent")), "ups": True}
            with mock.patch.object(db, "UPS_FILE", self.info):
                self.assertIn("ups", checks(s))
                self.assertNotIn("ups", checks({**s, "ups": False}))
            with mock.patch.object(db, "UPS_FILE", self.tmp / "missing"):
                self.assertNotIn("ups", checks(s))  # setup.sh didn't set NUT up

    def test_render(self):
        status = {"time": time.time(), "hostname": "box", "storage_root": "/mnt/storage",
                  "checks": {"ups": self.check(ON_MAINS)}}
        page = db.render(status, "box")
        self.assertIn('id="ups"', page)
        self.assertIn("<b>UPS</b>", page)
        self.assertIn("Grid: no power cuts", page)
        self.assertIsNone(db.ALERT_FROM["ups"])  # upsmon pushes itself


if __name__ == "__main__":
    unittest.main()
