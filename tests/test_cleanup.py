import logging
import os
import sqlite3
import unittest

from helpers import RunCase

logging.disable(logging.CRITICAL)
HOUR = 3600


class CleanupCase(RunCase):
    def setUp(self):
        super().setUp()
        self.run_once()  # first run: seeds the feed and finished downloads

    def download(self, name, files=("movie.mkv", "movie.nfo"), size=10_000_000_000,
                 ratio=0.1, seeded_h=1, folder="downloads/movies"):
        """A finished arr download with real files; returns (torrent dict, its folder)."""
        directory = self.tmp / "storage" / folder
        for f in files:
            (directory / name / f).parent.mkdir(parents=True, exist_ok=True)
            (directory / name / f).write_text("x")
        info_hash = self.server.add_finished(name, size=size, download_dir=str(directory))
        t = self.server.tr_torrents[-1]
        t.update(files=[{"name": f"{name}/{f}", "length": 1} for f in files],
                 uploadRatio=ratio, secondsSeeding=seeded_h * HOUR)
        return t, directory / name, info_hash

    def link(self, folder, *files):
        """Import: hardlink the files into the library. Returns the library paths."""
        library = self.tmp / "storage" / "MOVIES" / folder.name
        library.mkdir(parents=True, exist_ok=True)
        paths = []
        for f in files:
            os.link(folder / f, library / f.replace("/", "_"))
            paths.append(library / f.replace("/", "_"))
        return paths

    def arr_run(self, **changes):
        return self.run_once(arr=True, **changes)


class CleanupTest(CleanupCase):
    """Sonarr/Radarr/Whisparr downloads are removed once the library let go of them and
    seeding is done; nothing else is. (With minimal_seed off: see MinimalSeedTest for on.)"""

    def run_once(self, *args, **changes):
        changes.setdefault("minimal_seed", False)
        return super().run_once(*args, **changes)

    def test_replaced_movie_is_removed_once_seeded(self):
        t, folder, h = self.download("Crank.II.2009.1080p.Remux", seeded_h=10)
        library = self.link(folder, "movie.mkv")
        self.arr_run()                       # imported: remembered
        self.assertEqual(self.server.removed, [])
        library[0].unlink()                  # Radarr replaced it
        self.arr_run()                       # 10 h seeded: not yet
        self.assertEqual(self.server.removed, [])
        t["secondsSeeding"] = 70 * HOUR      # 50 h + 0.4 h x 10 GB = 54 h
        report = self.arr_run()
        self.assertEqual(self.server.removed, [(h, True)])
        self.assertEqual([r["name"] for r in report.removed], ["Crank.II.2009.1080p.Remux"])
        push = self.server.pushes[-1]
        self.assertEqual(push["title"], "Removed: Crank.II.2009.1080p.Remux")
        self.assertEqual(push["message"], "9.3 GB freed, ratio 0.10, seeded 70 h")
        self.assertEqual((push["priority"], push["tags"]), (2, ["wastebasket"]))
        self.arr_run()
        self.assertEqual(len(self.server.removed), 1)

    def test_ratio_is_enough(self):
        t, folder, h = self.download("A", ratio=1.5)
        library = self.link(folder, "movie.mkv")
        self.arr_run()
        library[0].unlink()
        self.arr_run()
        self.assertEqual(self.server.removed, [])
        t["uploadRatio"] = 2.0
        self.arr_run()
        self.assertEqual(self.server.removed, [(h, True)])

    def test_bigger_torrent_seeds_longer(self):
        t, folder, h = self.download("Big", size=34_000_000_000, seeded_h=60)
        library = self.link(folder, "movie.mkv")
        self.arr_run()
        library[0].unlink()
        self.arr_run()                       # needs 50 + 13.6 h
        self.assertEqual(self.server.removed, [])
        t["secondsSeeding"] = 64 * HOUR
        self.arr_run()
        self.assertEqual(self.server.removed, [(h, True)])

    def test_still_in_library_keeps_seeding(self):
        _, folder, _ = self.download("Kept", ratio=5, seeded_h=500)
        self.link(folder, "movie.mkv")
        self.arr_run()
        self.arr_run()
        self.assertEqual(self.server.removed, [])

    def test_never_imported_is_never_removed(self):
        self.download("Failed.Import", ratio=5, seeded_h=500)
        self.arr_run()
        self.arr_run()
        self.assertEqual(self.server.removed, [])

    def test_partly_imported_counts_as_not_imported(self):
        _, folder, _ = self.download("Pack", files=("e01.mkv", "e02.mkv"), ratio=5)
        library = self.link(folder, "e01.mkv")
        self.arr_run()
        library[0].unlink()
        self.arr_run()
        self.assertEqual(self.server.removed, [])

    def test_one_episode_still_in_library_keeps_the_pack(self):
        _, folder, _ = self.download("Pack", files=("e01.mkv", "e02.mkv"), ratio=5)
        library = self.link(folder, "e01.mkv", "e02.mkv")
        self.arr_run()
        library[0].unlink()                  # e01 upgraded, e02 still from this pack
        self.arr_run()
        self.assertEqual(self.server.removed, [])
        library[1].unlink()
        self.arr_run()
        self.assertEqual(len(self.server.removed), 1)

    def test_sample_and_extras_need_no_import(self):
        _, folder, h = self.download("B", files=("b.mkv", "Sample/b-sample.mkv", "b.nfo"),
                                     ratio=3)
        library = self.link(folder, "b.mkv")
        self.arr_run()
        library[0].unlink()
        self.arr_run()
        self.assertEqual(self.server.removed, [(h, True)])

    def test_missing_files_decide_nothing(self):
        _, folder, _ = self.download("C", ratio=3)
        library = self.link(folder, "movie.mkv")
        self.arr_run()
        library[0].unlink()
        (folder / "movie.nfo").unlink()
        self.arr_run()
        self.assertEqual(self.server.removed, [])

    def test_outside_the_arr_downloads_never_touched(self):
        _, folder, _ = self.download("Rss.Movie", ratio=9, seeded_h=900, folder="MOVIES/HD/HU")
        library = self.link(folder, "movie.mkv")
        self.arr_run()
        library[0].unlink()
        self.arr_run()
        self.assertEqual(self.server.removed, [])

    def test_off_without_arr_or_when_switched_off(self):
        _, folder, _ = self.download("D", ratio=3)
        library = self.link(folder, "movie.mkv")
        self.arr_run()
        library[0].unlink()
        self.run_once()                      # arr = no
        self.arr_run(remove_unused=False)
        self.assertEqual(self.server.removed, [])

    def test_dry_run_removes_nothing(self):
        _, folder, _ = self.download("E", ratio=3)
        library = self.link(folder, "movie.mkv")
        self.arr_run()
        library[0].unlink()
        report = self.arr_run(dry_run=True)
        self.assertEqual((self.server.removed, report.removed), ([], []))
        self.arr_run()
        self.assertEqual(len(self.server.removed), 1)

    def test_dry_run_on_a_state_file_from_before(self):
        db = sqlite3.connect(self.state_path)
        db.execute("DROP TABLE imported")
        db.commit()
        db.close()
        self.download("F", ratio=3)
        self.assertEqual(self.arr_run(dry_run=True).errors, [])
        self.arr_run()  # a real run adds the table

    def test_several_in_one_push(self):
        for name in ("F", "G"):
            _, folder, _ = self.download(name, ratio=3, size=5_000_000_000)
            self.link(folder, "movie.mkv")
        self.arr_run()
        for f in (self.tmp / "storage" / "MOVIES").glob("*/movie.mkv"):
            f.unlink()
        self.arr_run(notify_downloaded=False)
        push = self.server.pushes[-1]
        self.assertEqual(push["title"], "2 seeded downloads removed, 9.3 GB freed")
        self.assertEqual(push["message"].count("•"), 2)

    def test_notify_removed_off(self):
        _, folder, _ = self.download("H", ratio=3)
        library = self.link(folder, "movie.mkv")
        self.arr_run()
        pushes = len(self.server.pushes)
        library[0].unlink()
        self.arr_run(notify_removed=False)
        self.assertEqual((len(self.server.removed), len(self.server.pushes)), (1, pushes))


class MinimalSeedTest(CleanupCase):
    """minimal_seed (on by default): every download leaves Transmission once seeding is done."""

    def test_imported_goes_with_files_once_seeded(self):
        t, folder, h = self.download("Kept", seeded_h=10)
        library = self.link(folder, "movie.mkv")
        self.arr_run()
        self.assertEqual(self.server.removed, [])
        t["uploadRatio"] = 2.0
        self.arr_run()
        self.assertEqual(self.server.removed, [(h, True)])
        self.assertEqual(self.server.pushes[-1]["message"], "files kept, ratio 2.00, seeded 10 h")
        self.assertTrue(library[0].exists())

    def test_never_imported_still_stays(self):
        self.download("Failed.Import", ratio=5, seeded_h=500)
        self.arr_run()
        self.assertEqual(self.server.removed, [])

    def test_outside_arr_downloads_keeps_files(self):
        _, folder, h = self.download("Rss.Movie", ratio=0.5, seeded_h=60, size=1_000_000_000,
                                     folder="MOVIES/HD/HU")
        self.run_once()                      # arr = no: 50.4 h needed
        self.assertEqual(self.server.removed, [(h, False)])
        self.assertTrue((folder / "movie.mkv").exists())

    def test_mixed_push(self):
        _, folder, _ = self.download("F", ratio=3, size=5_000_000_000)
        library = self.link(folder, "movie.mkv")
        self.arr_run(minimal_seed=False)  # imported, but not removed yet
        library[0].unlink()
        self.download("Rss", ratio=3, folder="MOVIES/HD/HU")
        self.arr_run(notify_downloaded=False)
        self.assertEqual(self.server.pushes[-1]["title"],
                         "2 seeded downloads removed, 4.7 GB freed")
        self.download("Rss2", ratio=3, folder="MOVIES/HD/EN")
        self.download("Rss3", ratio=3, folder="MOVIES/HD/EN")
        self.arr_run(notify_downloaded=False)
        self.assertEqual(self.server.pushes[-1]["title"], "2 seeded downloads removed")

    def test_not_yet_seeded_stays(self):
        self.download("Rss", ratio=1, seeded_h=20, folder="MOVIES/HD/HU")
        self.run_once()
        self.assertEqual(self.server.removed, [])

    def test_off_keeps_seeding(self):
        self.download("Rss", ratio=3, folder="MOVIES/HD/HU")
        self.run_once(minimal_seed=False)
        self.assertEqual(self.server.removed, [])

    def test_dry_run_removes_nothing(self):
        self.download("Rss", ratio=3, folder="MOVIES/HD/HU")
        report = self.run_once(dry_run=True)
        self.assertEqual((self.server.removed, report.removed), ([], []))


if __name__ == "__main__":
    unittest.main()
