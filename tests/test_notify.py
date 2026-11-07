import logging
import shutil
import tempfile
import unittest
from collections import namedtuple
from pathlib import Path
from unittest import mock

from helpers import OLD, FakeServer, RunCase, rss2transmission as r2t, write_config

logging.disable(logging.CRITICAL)

Usage = namedtuple("Usage", "total used free")
GB = 1024 ** 3


class AddedPushTest(RunCase):
    def test_single_added(self):
        self.run_once()
        self.server.set_items([("102", "hd_hun", "Szuzlanyok_ajandeka_1080i")] + OLD)
        self.run_once()
        self.assertEqual(len(self.server.pushes), 1)
        push = self.server.pushes[0]
        self.assertEqual(push["title"], "Added: Szuzlanyok_ajandeka_1080i")
        self.assertEqual(push["message"], "→ MOVIES/HD/HU")
        self.assertEqual((push["priority"], push["tags"]), (2, ["arrow_down"]))

    def test_non_ascii_names_survive(self):
        # nCore filenames are ASCII, but Transmission's torrent names can be anything
        self.run_once()
        self.server.add_finished("Szűzlányok ajándéka 1080i", size=2 * GB)
        self.run_once()
        self.assertEqual(self.server.pushes[0]["title"], "Downloaded: Szűzlányok ajándéka 1080i")

    def test_several_added_and_duplicate_note(self):
        self.run_once()
        self.server.set_items([("103", "hd", "B"), ("102", "mp3", "A")] + OLD)
        self.run_once()
        self.server.set_items([("104", "hd", "B")] + [("103", "hd", "B"), ("102", "mp3", "A")] + OLD)
        self.server.torrents["104"] = self.server.torrents["103"]  # same torrent, new feed id
        self.run_once()
        titles = [p["title"] for p in self.server.pushes]
        self.assertEqual(titles, ["2 torrents added", "Added: B"])
        self.assertEqual(self.server.pushes[0]["message"],
                         "• A → MUSIC/LOSSY/EN\n• B → MOVIES/HD/EN")
        self.assertEqual(self.server.pushes[1]["message"],
                         "→ MOVIES/HD/EN (was already in Transmission)")

    def test_notify_added_off(self):
        self.run_once()
        self.server.set_items([("102", "hd", "A")] + OLD)
        self.assertEqual(len(self.run_once(notify_added=False).added), 1)
        self.assertEqual(self.server.pushes, [])

    def test_ntfy_down_is_an_error_but_torrent_stays_added(self):
        self.run_once()
        self.server.set_items([("102", "hd", "A")] + OLD)
        self.server.ntfy_down = True
        report = self.run_once()
        self.assertEqual((len(report.added), len(report.errors)), (1, 1))
        self.server.ntfy_down = False
        self.assertEqual(self.run_once().added, [])  # not re-added


class WarningsTest(RunCase):
    def test_mount_warning_once_per_day_and_reset_when_back(self):
        self.run_once()
        self.run_once(require_mount=True)
        self.run_once(require_mount=True)
        warnings = [p for p in self.server.pushes if p["title"] == "Storage disk not mounted"]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["priority"], 4)
        with mock.patch("rss2transmission.os.path.ismount", return_value=True):
            self.run_once(require_mount=True)  # back -> warning state cleared
        self.run_once(require_mount=True)
        self.assertEqual(sum(p["title"] == "Storage disk not mounted" for p in self.server.pushes), 2)

    def test_mount_warning_repeats_after_24h(self):
        self.run_once(require_mount=True)
        state = r2t.State(self.state_path)
        state.set_meta("mount_warned_at", "2000-01-01T00:00:00+00:00")
        state.close()
        self.run_once(require_mount=True)
        self.assertEqual(len(self.server.pushes), 2)

    def test_disk_warning(self):
        self.run_once()
        full = Usage(total=1000 * GB, used=950 * GB, free=50 * GB)
        with mock.patch("rss2transmission.shutil.disk_usage", return_value=full):
            report = self.run_once()
            self.run_once()
        self.assertEqual(report.errors, [])  # a warning, not a failure
        self.assertEqual(len(self.server.pushes), 1)
        self.assertEqual(self.server.pushes[0]["title"], "Storage disk 95% full")
        self.assertIn("50.0 GB free of 1000.0 GB", self.server.pushes[0]["message"])
        ok = Usage(total=1000 * GB, used=500 * GB, free=500 * GB)
        with mock.patch("rss2transmission.shutil.disk_usage", return_value=ok):
            self.run_once()
        with mock.patch("rss2transmission.shutil.disk_usage", return_value=full):
            self.run_once()
        self.assertEqual(len(self.server.pushes), 2)  # warned again after recovering

    def test_disk_percent_uses_df_formula(self):
        # 10 GB reserved for root: df shows used/(used+free) = 90/100 = 90%
        usage = Usage(total=110 * GB, used=90 * GB, free=10 * GB)
        with mock.patch("rss2transmission.shutil.disk_usage", return_value=usage):
            self.run_once()
        self.assertEqual(self.server.pushes[0]["title"], "Storage disk 90% full")

    def test_disk_percent_rounds_up_like_df(self):
        usage = Usage(total=1000 * GB, used=891 * GB, free=109 * GB)  # 89.1% -> df says 90%
        with mock.patch("rss2transmission.shutil.disk_usage", return_value=usage):
            self.run_once()
        self.assertEqual(self.server.pushes[0]["title"], "Storage disk 90% full")


class FormattingTest(unittest.TestCase):
    def test_display_name(self):
        self.assertEqual(r2t.display_name("[nCore][hd_hun]Some.Movie.torrent"), "Some.Movie")
        self.assertEqual(r2t.display_name("manual.torrent"), "manual")

    def test_human_size(self):
        self.assertEqual(r2t.human_size(512), "512 B")
        self.assertEqual(r2t.human_size(1536), "1.5 KB")
        self.assertEqual(r2t.human_size(7_730_000_000), "7.2 GB")
        self.assertEqual(r2t.human_size(3 * 1024 ** 4), "3.0 TB")

    def test_bullet_list_truncates(self):
        text = r2t.bullet_list([f"item {i}" for i in range(25)])
        self.assertEqual(text.count("•"), 20)
        self.assertTrue(text.endswith("… and 5 more"))


class NtfyAndCliTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)
        self.server = FakeServer().__enter__()
        self.config = write_config(self.tmp, self.server.base_url)

    def tearDown(self):
        self.server.__exit__()
        self.tmpdir.cleanup()

    def test_test_notify(self):
        self.assertEqual(r2t.main(["-c", str(self.config), "--test-notify"]), 0)
        self.assertEqual(self.server.pushes[0]["title"], "torrent-box test")
        self.assertFalse((self.tmp / "state").exists())  # no run happened

    def test_test_notify_failures(self):
        self.server.ntfy_down = True
        self.assertEqual(r2t.main(["-c", str(self.config), "--test-notify"]), 1)
        secrets = self.tmp / "secrets.ini"
        secrets.write_text(secrets.read_text().replace("ntfy_topic   = test-topic", ""))
        self.assertEqual(r2t.main(["-c", str(self.config), "--test-notify"]), 2)

    def test_bad_priority_is_a_config_error(self):
        self.config.write_text(self.config.read_text().replace(
            "[ntfy]\n", "[ntfy]\npriority_added = loud\n"))
        with self.assertRaisesRegex(r2t.ConfigError, "priority_added"):
            r2t.load_config(self.config)

    def test_example_config_values(self):
        d = self.tmp / "example"
        d.mkdir()
        root = Path(__file__).resolve().parents[1]
        shutil.copy(root / "config.example.ini", d / "config.ini")
        shutil.copy(root / "secrets.example.ini", d / "secrets.ini")
        cfg = r2t.load_config(d / "config.ini")
        self.assertEqual((cfg.priority_added, cfg.priority_downloaded, cfg.priority_problem),
                         (2, 3, 4))
        self.assertEqual(cfg.ntfy_server, "https://ntfy.sh")
        self.assertTrue(cfg.notify_added and cfg.notify_downloaded)


if __name__ == "__main__":
    unittest.main()
