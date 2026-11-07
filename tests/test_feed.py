import unittest

from helpers import FIXTURES, rss2transmission as r2t


class ParseFeedTest(unittest.TestCase):
    def setUp(self):
        self.items = r2t.parse_feed((FIXTURES / "feed.xml").read_bytes())

    def test_oldest_first(self):
        self.assertEqual([i.ncore_id for i in self.items], ["1000001", "1000002", "1000003"])

    def test_fields(self):
        item = self.items[-1]
        self.assertEqual(item.title,
                         "Bud.Spencer.Terence.Hill.Bosszu.El.Pasoban.XviD.Hun-Coopter [Film (HUN SD)]")
        self.assertEqual(item.link, "https://ncore.pro/rss_dl.php/id=1000003/key=REDACTEDKEY")

    def test_unicode_title(self):
        self.assertEqual(self.items[0].title, "Szűzlányok ajándéka 1080i [Film (HUN HD)]")

    def test_items_without_link_are_skipped_and_no_id_falls_back_to_link(self):
        xml = b"""<rss><channel>
            <item><title>no link</title></item>
            <item><title>odd</title><link>https://x/dl/abc/key=SECRET</link></item>
        </channel></rss>"""
        items = r2t.parse_feed(xml)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].ncore_id, "https://x/dl/abc")  # passkey stripped

    def test_empty_feed(self):
        self.assertEqual(r2t.parse_feed(b"<rss><channel></channel></rss>"), [])


if __name__ == "__main__":
    unittest.main()
