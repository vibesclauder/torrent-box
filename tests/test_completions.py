import logging
import unittest

from helpers import OLD, RunCase

logging.disable(logging.CRITICAL)


class CompletionsTest(RunCase):
    def test_first_run_does_not_announce_already_finished(self):
        self.server.add_finished("Already.Done.2020")
        report = self.run_once()
        self.assertEqual((report.seeded_completions, report.completed), (1, []))
        self.assertEqual(self.server.pushes, [])
        self.assertEqual(self.run_once().completed, [])
        self.assertEqual(self.server.pushes, [])

    def test_finished_download_is_announced_once(self):
        self.run_once()
        self.server.add_finished("Dune.Part.Two.2024.1080p", size=7_730_000_000,
                                 download_dir=self.storage("MOVIES/HD/HU"))
        report = self.run_once()
        self.assertEqual([t["name"] for t in report.completed], ["Dune.Part.Two.2024.1080p"])
        self.assertEqual(len(self.server.pushes), 1)
        push = self.server.pushes[0]
        self.assertEqual(push["title"], "Downloaded: Dune.Part.Two.2024.1080p")
        self.assertEqual(push["message"], "7.2 GB → MOVIES/HD/HU")
        self.assertEqual((push["topic"], push["priority"], push["tags"]),
                         ("test-topic", 3, ["white_check_mark"]))
        self.run_once()
        self.assertEqual(len(self.server.pushes), 1)  # not again

    def test_added_then_finished_later(self):
        self.run_once()
        self.server.set_items([("102", "hd", "New.Film")] + OLD)
        self.run_once()                      # added -> one "Added" push
        self.assertEqual(self.run_once().completed, [])  # still downloading
        self.server.finish_all()
        report = self.run_once()
        self.assertEqual(len(report.completed), 1)
        self.assertEqual([p["title"].split(":")[0] for p in self.server.pushes],
                         ["Added", "Downloaded"])

    def test_several_finished_in_one_push(self):
        self.run_once()
        for name in ("A", "B", "C"):
            self.server.add_finished(name)
        self.run_once()
        self.assertEqual(len(self.server.pushes), 1)
        self.assertEqual(self.server.pushes[0]["title"], "3 downloads finished")
        self.assertEqual(self.server.pushes[0]["message"].count("•"), 3)
        # outside the storage root -> full path is shown
        self.assertIn("• A (4.2 GB → /mnt/storage/MOVIES/HD/HU)", self.server.pushes[0]["message"])

    def test_ntfy_down_retries_next_run(self):
        self.run_once()
        self.server.add_finished("X")
        self.server.ntfy_down = True
        report = self.run_once()
        self.assertEqual((report.completed, len(report.errors)), ([], 1))
        self.server.ntfy_down = False
        self.assertEqual(len(self.run_once().completed), 1)
        self.assertEqual(len(self.server.pushes), 1)

    def test_notify_downloaded_off_still_marks_announced(self):
        self.run_once()
        self.server.add_finished("X")
        self.run_once(notify_downloaded=False)
        self.assertEqual(self.server.pushes, [])
        self.run_once()  # switching it back on later doesn't flood old ones
        self.assertEqual(self.server.pushes, [])

    def test_no_topic_means_no_pushes(self):
        self.run_once()
        self.server.add_finished("X")
        report = self.run_once(topic="")
        self.assertEqual((len(report.completed), report.errors), (1, []))
        self.assertEqual(self.server.pushes, [])

    def test_transmission_down_skips_completions(self):
        self.run_once()
        self.server.add_finished("X")
        self.server.rpc_down = True
        report = self.run_once()
        self.assertEqual(report.completed, [])
        self.server.rpc_down = False
        self.assertEqual(len(self.run_once().completed), 1)

    def test_catch_up_reseeds_completions(self):
        self.run_once()
        self.server.add_finished("X")
        report = self.run_once(catch_up=True)
        self.assertEqual((report.seeded_completions, report.completed), (1, []))
        self.assertEqual(self.run_once().completed, [])
        self.assertEqual(self.server.pushes, [])

    def test_dry_run_neither_pushes_nor_marks(self):
        self.run_once()
        self.server.add_finished("X")
        report = self.run_once(dry_run=True)
        self.assertEqual(len(report.completed), 1)
        self.assertEqual(self.server.pushes, [])
        self.assertEqual(len(self.run_once().completed), 1)  # real run still announces it


if __name__ == "__main__":
    unittest.main()
