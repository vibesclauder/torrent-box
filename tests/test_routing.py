import re
import tempfile
import unittest
from pathlib import Path

from helpers import rss2transmission as r2t, write_config

# The routing chain from the old periodic.sh, in its original order: first regex that matches
# anywhere in the filename wins, otherwise MISC. Kept here as the reference to stay compatible.
PERIODIC_SH = [
    (r"\[nCore\]\[console", "GAMES/CNS"),
    (r"\[nCore\]\[game", "GAMES/PC"),
    (r"\[nCore\]\[lossless_hun", "MUSIC/LOSSLESS/HU"),
    (r"\[nCore\]\[lossless\]", "MUSIC/LOSSLESS/EN"),
    (r"\[nCore\]\[mp3_hun", "MUSIC/LOSSY/HU"),
    (r"\[nCore\]\[mp3\]", "MUSIC/LOSSY/EN"),
    (r"\[nCore\]\[hd_hun", "MOVIES/HD/HU"),
    (r"\[nCore\]\[hd\]", "MOVIES/HD/EN"),
    (r"\[nCore\]\[xvid_hun", "MOVIES/SD/HU"),
    (r"\[nCore\]\[xvid\]", "MOVIES/SD/EN"),
    (r"\[nCore\]\[hdser_hun", "SERIES/HD/HU"),
    (r"\[nCore\]\[hdser\]", "SERIES/HD/EN"),
    (r"\[nCore\]\[xvidser\]", "SERIES/SD/EN"),
    (r"\[nCore\]\[xvidser_hun", "SERIES/SD/HU"),
    (r"\[nCore\]\[xxx_hd", "XXX/HD"),
    (r"\[nCore\]\[xxx_xvid", "XXX/SD"),
    (r"\[nCore\]\[xxx_imageset", "XXX/IMG"),
    (r"\[nCore\]\[ebook\]", "EBOOKS/EN"),
    (r"\[nCore\]\[ebook_hun", "EBOOKS/HU"),
    (r"\[nCore\]\[[im][si][os]", "PROGRAMS/PC"),
    (r"\[nCore\]\[mobil", "PROGRAMS/MBL"),
]

# Every category code nCore uses.
NCORE_CODES = [
    "xvid_hun", "xvid", "dvd_hun", "dvd", "dvd9_hun", "dvd9", "hd_hun", "hd",
    "xvidser_hun", "xvidser", "dvdser_hun", "dvdser", "hdser_hun", "hdser",
    "mp3_hun", "mp3", "lossless_hun", "lossless", "clip",
    "game_iso", "game_rip", "console", "ebook_hun", "ebook",
    "iso", "misc", "mobil", "xxx_xvid", "xxx_dvd", "xxx_imageset", "xxx_hd",
]


def periodic_sh_route(filename):
    import re
    for pattern, folder in PERIODIC_SH:
        if re.search(pattern, filename):
            return folder
    return "MISC"


class RoutingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = r2t.load_config(write_config(Path(self.tmp.name), "http://unused"))

    def tearDown(self):
        self.tmp.cleanup()

    def folder(self, filename):
        return str(r2t.route(self.cfg, filename).relative_to(self.cfg.storage_root))

    def test_category_extraction(self):
        self.assertEqual(r2t.ncore_category("[nCore][hd_hun]Dune.2021.torrent"), "hd_hun")
        self.assertEqual(r2t.ncore_category("[nCore][xxx_imageset]A.B.torrent"), "xxx_imageset")
        self.assertIsNone(r2t.ncore_category("some.other.tracker.torrent"))
        self.assertIsNone(r2t.ncore_category("x[nCore][hd]late.prefix.torrent"))

    def test_matches_periodic_sh_for_every_ncore_code(self):
        for code in NCORE_CODES:
            name = f"[nCore][{code}]Some.Release.Name-GRP.torrent"
            with self.subTest(code=code):
                self.assertEqual(self.folder(name), periodic_sh_route(name))

    def test_unknown_and_foreign_go_to_misc(self):
        self.assertEqual(self.folder("[nCore][brand_new_cat]X.torrent"), "MISC")
        self.assertEqual(self.folder("ubuntu-24.04-desktop-amd64.iso.torrent"), "MISC")

    def test_route_is_under_storage_root(self):
        self.assertEqual(r2t.route(self.cfg, "[nCore][hd]X.torrent"),
                         self.cfg.storage_root / "MOVIES/HD/EN")


class ShowFolderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = r2t.load_config(write_config(Path(self.tmp.name), "http://unused"))
        self.series = self.cfg.storage_root / "SERIES/HD/HU"

    def tearDown(self):
        self.tmp.cleanup()

    def test_show_name(self):
        cases = {
            "The.Last.of.Us.S02E03.1080p.HMAX.WEB-DL.DDP5.1.H.264.HUN.ENG-PepeLeP": "The Last of Us",
            "The.Last.of.Us.S02.1080p.HMAX.WEB-DL.HUN-PepeLeP": "The Last of Us",
            "Doctor.Who.2005.S01.720p.BluRay.x264.HUN-XYZ": "Doctor Who (2005)",
            "S.W.A.T.2017.S07E01.1080p.WEB.h264-ETHEL": "S.W.A.T (2017)",
            "Marvels.Agents.of.S.H.I.E.L.D.S01.720p": "Marvels Agents of S.H.I.E.L.D",
            "A.Mandalori.S03E01.2160p.DSNP.WEB-DL.HUN": "A Mandalori",
            "Bluey.S01-S03.1080p.WEB-DL.HUN": "Bluey",
            "1923.S02E01.2160p.WEB-DL": "1923",  # a title that looks like a year
            "9-1-1.S08E01.1080p": "9-1-1",
            "Stargate.SG-1.S01E01": "Stargate SG-1",
            "Monk.Season.1.DVDRip": "Monk",
            "Szomszedok.1.evad.DVDRip.HUN": "Szomszedok",
            "Kisvaros 2. évad 720p": "Kisvaros",
            "mr.robot.s04e13.1080p": "mr robot",
        }
        for release, show in cases.items():
            with self.subTest(release=release):
                self.assertEqual(r2t.show_name(release), show)

    def test_no_show_name(self):
        for release in ("Some.Show.Complete.Series.1080p", "S01E01.Pilot.1080p",
                        "Dune.Part.Two.2024.1080p", "Super.Mario.Bros.1993"):
            with self.subTest(release=release):
                self.assertIsNone(r2t.show_name(release))

    def test_series_get_a_folder_per_show(self):
        self.assertEqual(r2t.route(self.cfg, "[nCore][hdser_hun]The.Last.of.Us.S02E03.1080p.torrent"),
                         self.series / "The Last of Us")
        self.assertEqual(r2t.route(self.cfg, "[nCore][xvidser]Monk.S01.DVDRip.torrent"),
                         self.cfg.storage_root / "SERIES/SD/EN/Monk")

    def test_series_without_marker_and_other_categories_unchanged(self):
        self.assertEqual(r2t.route(self.cfg, "[nCore][hdser_hun]Some.Show.Complete.1080p.torrent"),
                         self.series)
        self.assertEqual(r2t.route(self.cfg, "[nCore][hd_hun]Show.S01.Not.Really.torrent"),
                         self.cfg.storage_root / "MOVIES/HD/HU")

    def test_existing_show_folder_is_reused(self):
        (self.series / "The Last Of Us (2023)").mkdir(parents=True)
        (self.series / "The.Last.of.Us.S01.1080p-OLD").mkdir()  # an old season pack: not it
        self.assertEqual(r2t.route(self.cfg, "[nCore][hdser_hun]The.Last.of.Us.S02E03.torrent"),
                         self.series / "The Last Of Us (2023)")
        (self.series / "Doctor Who").mkdir()
        self.assertEqual(r2t.route(self.cfg, "[nCore][hdser_hun]Doctor.Who.2005.S02.torrent"),
                         self.series / "Doctor Who")

    def test_show_folders_setting(self):
        config = Path(self.tmp.name) / "config.ini"
        text = config.read_text()
        config.write_text(text.replace("[general]\n", "[general]\nshow_folders =\n"))
        self.assertEqual(r2t.load_config(config).show_categories, frozenset())
        self.assertEqual(r2t.route(r2t.load_config(config), "[nCore][hdser]Monk.S01.torrent"),
                         self.cfg.storage_root / "SERIES/HD/EN")
        config.write_text(text.replace("[general]\n", "[general]\nshow_folders = hdser, dvdser\n"))
        self.assertEqual(r2t.load_config(config).show_categories, {"hdser", "dvdser"})
        # not set at all: nCore's series categories
        self.assertIn("hdser_hun", self.cfg.show_categories)
        self.assertNotIn("hd_hun", self.cfg.show_categories)

    def test_another_year_is_another_show(self):
        (self.series / "Doctor Who (1963)").mkdir(parents=True)
        self.assertEqual(r2t.route(self.cfg, "[nCore][hdser_hun]Doctor.Who.2005.S02.torrent"),
                         self.series / "Doctor Who (2005)")
        (self.series / "Doctor Who (2005)").mkdir()
        self.assertEqual(r2t.route(self.cfg, "[nCore][hdser_hun]Doctor.Who.2005.S03.torrent"),
                         self.series / "Doctor Who (2005)")
        # no year in the release: any of them; one without a year comes first
        self.assertEqual(r2t.route(self.cfg, "[nCore][hdser_hun]Doctor.Who.S01.torrent"),
                         self.series / "Doctor Who (1963)")
        (self.series / "Doctor Who").mkdir()
        self.assertEqual(r2t.route(self.cfg, "[nCore][hdser_hun]Doctor.Who.S01.torrent"),
                         self.series / "Doctor Who")

    def test_no_show_folders_in_sonarrs_root_folders_with_arr(self):
        # Sonarr would import a release filed into its "Show (2023)" folder in place and
        # delete it on an upgrade, while it's still seeding
        config = Path(self.tmp.name) / "config.ini"
        text = config.read_text()
        config.write_text(re.sub(r"(?m)^arr\s*=.*$", "arr = yes", text))
        cfg = r2t.load_config(config)
        for code in r2t.SONARR_ROUTES:
            self.assertNotIn(code, cfg.show_categories)
        self.assertIn("xvidser_hun", cfg.show_categories)  # not Sonarr's: unchanged
        (self.series / "The Last of Us (2023)").mkdir(parents=True)
        self.assertEqual(r2t.route(cfg, "[nCore][hdser_hun]The.Last.of.Us.S02E03.torrent"),
                         self.series)
        config.write_text(config.read_text().replace("arr = yes", "arr = no"))
        self.assertIn("hdser_hun", r2t.load_config(config).show_categories)

    def test_sonarr_routes_match_setup(self):
        setup = (Path(__file__).resolve().parents[1] / "setup.sh").read_text()
        self.assertIn(f"for code in {' '.join(r2t.SONARR_ROUTES)}; do", setup)


class HelpersTest(unittest.TestCase):
    def test_mask_hides_passkeys(self):
        text = ("https://ncore.pro/rss_dl.php/id=1/key=abc123 and "
                "https://ncore.pro/bookmarks/0123abcdFAKEKEY9")
        masked = r2t.mask(text)
        self.assertNotIn("abc123", masked)
        self.assertNotIn("0123abcdFAKEKEY9", masked)
        self.assertIn("id=1", masked)

    def test_safe_filename(self):
        self.assertEqual(r2t.safe_filename("../../etc/passwd"), "passwd.torrent")
        self.assertEqual(r2t.safe_filename("[nCore][hd]A.torrent"), "[nCore][hd]A.torrent")
        self.assertEqual(r2t.safe_filename(".hidden.torrent"), "hidden.torrent")
        self.assertEqual(r2t.safe_filename(""), "unnamed.torrent")


if __name__ == "__main__":
    unittest.main()
