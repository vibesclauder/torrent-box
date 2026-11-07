import fcntl
import logging
import tempfile
import unittest
from pathlib import Path

from helpers import OLD, FakeServer, RunCase, fake_torrent, rss2transmission as r2t, write_config

logging.disable(logging.CRITICAL)


class RunTest(RunCase):
    # --- first run / seeding

    def test_first_run_seeds_without_adding(self):
        report = self.run_once()
        self.assertEqual((report.seeded, report.added, report.errors), (2, [], []))
        self.assertEqual(self.server.added, {})
        # second run with the same feed: still nothing
        report = self.run_once()
        self.assertEqual((report.seeded, report.added), (0, []))

    def test_catch_up_reseeds(self):
        self.run_once()
        self.server.set_items([("102", "hd", "New.One")] + OLD)
        report = self.run_once(catch_up=True)
        self.assertEqual((report.seeded, report.added), (3, []))
        self.assertEqual(self.run_once().added, [])

    # --- normal operation

    def test_new_items_are_added_routed_and_archived(self):
        self.run_once()
        self.server.set_items([("103", "hdser", "Show.S01E02"), ("102", "mp3_hun", "Album")] + OLD)
        report = self.run_once()
        self.assertEqual(report.errors, [])
        self.assertEqual([a.filename for a in report.added],  # oldest first
                         ["[nCore][mp3_hun]Album.torrent", "[nCore][hdser]Show.S01E02.torrent"])
        self.assertEqual(self.server.added_dirs(),
                         sorted([self.storage("MUSIC/LOSSY/HU"), self.storage("SERIES/HD/EN/Show")]))
        self.assertTrue((self.tmp / "archive" / "[nCore][mp3_hun]Album.torrent").exists())
        self.assertEqual(self.run_once().added, [])  # now seen

    def test_duplicate_counts_as_done(self):
        self.run_once()
        self.server.set_items([("102", "hd", "Dup")] + OLD)
        r2t.Transmission(f"{self.server.base_url}/transmission/rpc", "user", "pass").add(
            fake_torrent("Dup"), "/elsewhere")
        report = self.run_once()
        self.assertEqual([a.status for a in report.added], ["duplicate"])
        self.assertEqual(self.run_once().added, [])

    # --- failures are retried

    def test_transmission_down_retries_next_run(self):
        self.run_once()
        self.server.set_items([("102", "hd", "New.One")] + OLD)
        (self.tmp / "watch" / "manual.torrent").write_bytes(fake_torrent("manual"))
        self.server.rpc_down = True
        report = self.run_once()
        self.assertEqual(report.added, [])
        self.assertEqual(len(report.errors), 1)  # stops after the first failure
        self.assertTrue((self.tmp / "watch" / "manual.torrent").exists())

        self.server.rpc_down = False
        report = self.run_once()
        self.assertEqual(sorted(a.source for a in report.added), ["feed", "watch"])

    def test_bad_download_skips_item_but_continues(self):
        self.run_once()
        self.server.set_items([("103", "hd", "Good"), ("102", "hd", "Broken")] + OLD)
        self.server.torrents["102"] = ("[nCore][hd]Broken.torrent", b"<html>login</html>")
        report = self.run_once()
        self.assertEqual([a.filename for a in report.added], ["[nCore][hd]Good.torrent"])
        self.assertEqual(len(report.errors), 1)
        self.assertNotIn("SECRETPASSKEY", report.errors[0])
        # fixed on nCore's side -> picked up next run
        self.server.torrents["102"] = ("[nCore][hd]Broken.torrent", fake_torrent("Broken"))
        self.assertEqual([a.filename for a in self.run_once().added], ["[nCore][hd]Broken.torrent"])

    def test_rejected_torrent_is_retried(self):
        self.run_once()
        self.server.set_items([("102", "hd", "X")] + OLD)
        self.server.rpc_result = "invalid or corrupt torrent file"
        report = self.run_once()
        self.assertEqual((report.added, len(report.errors)), ([], 1))
        self.server.rpc_result = None
        self.assertEqual(len(self.run_once().added), 1)

    def test_feed_down_still_processes_watch_dir(self):
        self.run_once()
        self.server.feed_status = 500
        (self.tmp / "watch" / "manual.torrent").write_bytes(fake_torrent("manual"))
        report = self.run_once()
        self.assertEqual(len(report.errors), 1)
        self.assertEqual([a.source for a in report.added], ["watch"])

    # --- watch dir

    def test_watch_dir(self):
        self.run_once()
        (self.tmp / "watch" / "[nCore][ebook]Book.torrent").write_bytes(fake_torrent("book"))
        (self.tmp / "watch" / "notes.txt").write_text("ignored")
        report = self.run_once()
        self.assertEqual([a.filename for a in report.added], ["[nCore][ebook]Book.torrent"])
        self.assertEqual(self.server.added_dirs(), [self.storage("EBOOKS/EN")])
        self.assertFalse((self.tmp / "watch" / "[nCore][ebook]Book.torrent").exists())
        self.assertTrue((self.tmp / "archive" / "[nCore][ebook]Book.torrent").exists())
        self.assertTrue((self.tmp / "watch" / "notes.txt").exists())

    def test_series_added_into_a_folder_per_show(self):
        self.run_once()
        name = "[nCore][hdser_hun]The.Last.of.Us.S02E03.1080p.WEB-DL.HUN.torrent"
        (self.tmp / "watch" / name).write_bytes(fake_torrent("tlou"))
        report = self.run_once()
        self.assertEqual(self.server.added_dirs(), [self.storage("SERIES/HD/HU/The Last of Us")])
        self.assertEqual(report.added[0].download_dir.name, "The Last of Us")

    # --- dry run

    def test_dry_run_changes_nothing(self):
        self.run_once()
        self.server.set_items([("102", "hd", "New.One")] + OLD)
        (self.tmp / "watch" / "manual.torrent").write_bytes(fake_torrent("manual"))
        before = self.state_path.read_bytes()
        report = self.run_once(dry_run=True)
        self.assertEqual(sorted(a.status for a in report.added), ["dry-run", "dry-run"])
        self.assertEqual(self.server.added, {})
        self.assertEqual(self.state_path.read_bytes(), before)
        self.assertTrue((self.tmp / "watch" / "manual.torrent").exists())
        self.assertFalse((self.tmp / "archive").exists())
        self.assertEqual(self.server.pushes, [])

    def test_dry_run_without_state_creates_no_state(self):
        report = self.run_once(dry_run=True)
        self.assertEqual(report.seeded, 2)
        self.assertFalse(self.state_path.exists())

    # --- mount check

    def test_unmounted_storage_adds_nothing(self):
        self.run_once()
        self.server.set_items([("102", "hd", "New.One")] + OLD)
        report = self.run_once(require_mount=True)  # tmp/storage is not a mountpoint
        self.assertEqual(report.added, [])
        self.assertIn("not mounted", report.errors[0])
        self.assertEqual(self.server.added, {})
        self.assertNotIn(("GET", "/feed"), self.server.requests[-3:])


class MainTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_missing_config_exits_2(self):
        self.assertEqual(r2t.main(["-c", str(self.tmp / "nope.ini")]), 2)

    def test_missing_secret_exits_2(self):
        config = write_config(self.tmp, "http://unused")
        (self.tmp / "secrets.ini").write_text("[secrets]\nfeed_url = x\n")
        self.assertEqual(r2t.main(["-c", str(config)]), 2)

    def test_full_run_and_lock(self):
        with FakeServer() as server:
            server.set_items(OLD)
            config = write_config(self.tmp, server.base_url)
            state = self.tmp / "st" / "state.sqlite"
            self.assertEqual(r2t.main(["-c", str(config), "--state", str(state)]), 0)
            self.assertTrue(state.exists())
            # a second run while another holds the lock just skips
            state.with_suffix(".lock").touch()
            with open(state.with_suffix(".lock")) as held:
                fcntl.flock(held, fcntl.LOCK_EX)
                server.requests.clear()
                self.assertEqual(r2t.main(["-c", str(config), "--state", str(state)]), 0)
                self.assertEqual(server.requests, [])
            # errors -> exit 1
            server.rpc_down = True
            server.set_items([("102", "hd", "New")] + OLD)
            self.assertEqual(r2t.main(["-c", str(config), "--state", str(state)]), 1)


if __name__ == "__main__":
    unittest.main()
