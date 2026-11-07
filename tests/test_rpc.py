import socket
import unittest

from helpers import FakeServer, fake_torrent, rss2transmission as r2t


class TransmissionRpcTest(unittest.TestCase):
    def setUp(self):
        self.server = FakeServer().__enter__()
        self.url = f"{self.server.base_url}/transmission/rpc"

    def tearDown(self):
        self.server.__exit__()

    def client(self, user="user", password="pass"):
        return r2t.Transmission(self.url, user, password, timeout=5)

    def test_session_id_handshake_then_add(self):
        tr = self.client()
        status, info_hash = tr.add(fake_torrent("a"), "/mnt/storage/MOVIES/HD/HU")
        self.assertEqual(status, "added")
        self.assertEqual(len(info_hash), 40)
        self.assertEqual(tr.session_id, FakeServer.SESSION_ID)
        self.assertEqual(self.server.added_dirs(), ["/mnt/storage/MOVIES/HD/HU"])
        # 1st POST got 409, 2nd succeeded; the next call reuses the session id directly
        tr.add(fake_torrent("b"), "/x")
        self.assertEqual(len([r for r in self.server.requests if r[0] == "POST"]), 3)

    def test_duplicate(self):
        tr = self.client()
        _, first = tr.add(fake_torrent("a"), "/x")
        status, again = tr.add(fake_torrent("a"), "/x")
        self.assertEqual((status, again), ("duplicate", first))

    def test_bad_login(self):
        with self.assertRaisesRegex(r2t.TransmissionUnavailable, "login refused"):
            self.client(password="wrong").add(fake_torrent("a"), "/x")

    def test_server_error(self):
        self.server.rpc_down = True
        with self.assertRaisesRegex(r2t.TransmissionUnavailable, "503"):
            self.client().call("session-get")

    def test_rejected_torrent(self):
        self.server.rpc_result = "invalid or corrupt torrent file"
        with self.assertRaisesRegex(r2t.TorrentRejected, "corrupt"):
            self.client().add(b"garbage", "/x")

    def test_connection_refused(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]  # closed again below, so nothing listens there
        tr = r2t.Transmission(f"http://127.0.0.1:{port}/transmission/rpc", "u", "p", timeout=5)
        with self.assertRaises(r2t.TransmissionUnavailable):
            tr.call("session-get")


if __name__ == "__main__":
    unittest.main()
