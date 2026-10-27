"""notify.transport -- the SSRF-safe HTTPS transport. No real network:
a local http.server on an ephemeral port, and "localhost" for DNS."""

import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from notify import transport, webhook  # noqa: E402


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
        status, _ = transport.http_post(url, b"{}", {})
        httpd.server_close()
        self.assertEqual(status, 302)


class DnsPinningTest(unittest.TestCase):
    def test_hostname_resolving_to_loopback_is_never_dialed(self):
        # is_safe_url passes DNS names by design; the transport is what
        # must refuse them once resolved.
        self.assertTrue(webhook.is_safe_url("https://localhost:9/h")[0])
        with self.assertRaises(transport.UnsafeDestination):
            transport.http_post("https://localhost:9/h", b"{}", {})


if __name__ == "__main__":
    unittest.main()
