"""platformd.serve -- the production entrypoint. No real network: the
redirect test runs a local http.server on an ephemeral port."""

import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from platformd import core, db, serve


class LoadCatalogTest(unittest.TestCase):
    def test_registers_drops_and_is_idempotent_across_boots(self):
        tmp = tempfile.mkdtemp()
        cat = os.path.join(tmp, "catalog.json")
        with open(cat, "w") as fh:
            json.dump([{"id": "d1", "title": "Drop one", "source": "x",
                        "risk_tier": "green"}], fh)
        conn = db.connect(os.path.join(tmp, "db.sqlite3"))
        self.assertEqual(serve.load_catalog(conn, cat), 1)
        self.assertEqual(serve.load_catalog(conn, cat), 1)  # second boot
        self.assertEqual(len(core.list_drops(conn)), 1)
        self.assertEqual(core.get_drop(conn, "d1")["title"], "Drop one")
        conn.close()


class BuildChannelsTest(unittest.TestCase):
    def test_every_selectable_channel_has_a_real_sender(self):
        # telegram is disabled in the UI and deliberately absent here
        self.assertEqual(set(serve.build_channels()),
                         {"discord", "ntfy", "webhook"})


class _Redirector(BaseHTTPRequestHandler):
    def do_POST(self):
        # drain the body first: closing with unread bytes sends RST, which
        # can beat the 302 to the client and flake this test
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(302)
        self.send_header("Location", "http://169.254.169.254/latest/")
        self.end_headers()

    def log_message(self, *args):
        pass


class HttpPostTest(unittest.TestCase):
    def test_redirect_is_returned_not_followed(self):
        # following a 30x would let a vetted webhook URL bounce the POST
        # past webhook.is_safe_url() to an internal address
        httpd = HTTPServer(("127.0.0.1", 0), _Redirector)
        threading.Thread(target=httpd.handle_request, daemon=True).start()
        url = "http://127.0.0.1:%d/hook" % httpd.server_address[1]
        status, _ = serve.http_post(url, b"{}", {})
        httpd.server_close()
        self.assertEqual(status, 302)


if __name__ == "__main__":
    unittest.main()
