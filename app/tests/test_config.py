"""app.config boot validation, deploy/.env.example staying in sync with
it, and serve's graceful shutdown (in-process and via a real SIGTERM)."""

import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from app import config, serve  # noqa: E402
from deploy import backup  # noqa: E402

LINK = "https://buy.stripe.com/test_abc"
SECRET = "whsec_test"
PUBLIC = "https://dropwatch.example"


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dw-config-")
        self.cg = os.path.join(self.tmp, "campgrounds.json")
        with open(self.cg, "w") as fh:
            json.dump([{"id": "1", "name": "A"}], fh)
        self.env = {"DROPWATCH_DB": os.path.join(self.tmp, "db", "x.sqlite3"),
                    "DROPWATCH_CAMPGROUNDS": self.cg}

    def problems(self, **extra):
        try:
            config.load(dict(self.env, **extra))
        except config.ConfigError as exc:
            return exc.problems
        return []

    def test_minimal_config_is_valid_and_typed(self):
        cfg = config.load(self.env)
        self.assertEqual(cfg["PORT"], 8787)
        self.assertFalse(cfg["DROPWATCH_TRUST_PROXY"])
        self.assertFalse(cfg["billing"])
        self.assertTrue(os.path.isdir(os.path.join(self.tmp, "db")))

    def test_full_billing_config_is_valid(self):
        cfg = config.load(dict(self.env, DROPWATCH_PAYMENT_LINK=LINK,
                               STRIPE_WEBHOOK_SECRET=SECRET,
                               DROPWATCH_PUBLIC_URL=PUBLIC + "/"))
        self.assertTrue(cfg["billing"])
        self.assertEqual(cfg["DROPWATCH_PUBLIC_URL"], PUBLIC)

    def test_payment_link_without_secret(self):
        p = self.problems(DROPWATCH_PAYMENT_LINK=LINK, DROPWATCH_PUBLIC_URL=PUBLIC)
        self.assertEqual(len(p), 1)
        self.assertIn("STRIPE_WEBHOOK_SECRET is not", p[0])

    def test_secret_without_payment_link(self):
        p = self.problems(STRIPE_WEBHOOK_SECRET=SECRET, DROPWATCH_PUBLIC_URL=PUBLIC)
        self.assertEqual(len(p), 1)
        self.assertIn("DROPWATCH_PAYMENT_LINK is not", p[0])

    def test_billing_requires_public_url(self):
        p = self.problems(DROPWATCH_PAYMENT_LINK=LINK, STRIPE_WEBHOOK_SECRET=SECRET)
        self.assertEqual(len(p), 1)
        self.assertIn("DROPWATCH_PUBLIC_URL is required", p[0])

    def test_unreadable_campground_index(self):
        p = self.problems(DROPWATCH_CAMPGROUNDS=os.path.join(self.tmp, "nope.json"))
        self.assertEqual(len(p), 1)
        self.assertIn("DROPWATCH_CAMPGROUNDS", p[0])
        with open(self.cg, "w") as fh:
            fh.write("{not json")
        self.assertIn("unreadable", self.problems()[0])

    @unittest.skipIf(os.geteuid() == 0, "root can write anywhere")
    def test_db_dir_not_writable(self):
        ro = os.path.join(self.tmp, "ro")
        os.mkdir(ro, 0o500)
        p = self.problems(DROPWATCH_DB=os.path.join(ro, "x.sqlite3"))
        self.assertEqual(len(p), 1)
        self.assertIn("not writable", p[0])

    def test_db_dir_not_creatable(self):
        blocker = os.path.join(self.tmp, "file")
        open(blocker, "w").close()
        p = self.problems(DROPWATCH_DB=os.path.join(blocker, "x.sqlite3"))
        self.assertEqual(len(p), 1)
        self.assertIn("not writable", p[0])

    def test_proxy_settings_must_agree(self):
        self.assertIn("ignored unless", self.problems(
            DROPWATCH_CLIENT_IP_HEADER="X-Real-IP")[0])
        self.assertEqual(self.problems(DROPWATCH_CLIENT_IP_HEADER="X-Real-IP",
                                       DROPWATCH_TRUST_PROXY="1"), [])
        self.assertIn("0 or 1", self.problems(DROPWATCH_TRUST_PROXY="yes")[0])

    def test_every_problem_is_reported_at_once(self):
        p = self.problems(PORT="eighty", STRIPE_WEBHOOK_SECRET="sk_live_oops",
                          DROPWATCH_CAMPGROUNDS="/nope")
        self.assertGreaterEqual(len(p), 4)

    def test_main_exits_2_with_a_clear_message(self):
        proc = subprocess.run(
            [sys.executable, "-m", "app.serve"], cwd=ROOT, capture_output=True,
            text=True, timeout=30,
            env=dict(os.environ, DROPWATCH_DB=self.env["DROPWATCH_DB"],
                     DROPWATCH_PAYMENT_LINK=LINK))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("invalid configuration", proc.stderr)
        self.assertIn("STRIPE_WEBHOOK_SECRET", proc.stderr)


class EnvExampleTest(unittest.TestCase):
    def test_env_example_documents_every_variable(self):
        with open(os.path.join(ROOT, "deploy", ".env.example")) as fh:
            text = fh.read()
        documented = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", text, re.M))
        expected = set(config.ENV_VARS) - {"HOST", "PORT", "DROPWATCH_DB",
                                           "DROPWATCH_CAMPGROUNDS"}
        expected |= set(backup.S3_VARS) | {"BACKUP_S3_PREFIX", "BACKUP_KEEP_DAYS"}
        self.assertEqual(expected - documented, set())

    def test_env_example_holds_no_real_secret(self):
        with open(os.path.join(ROOT, "deploy", ".env.example")) as fh:
            for line in fh:
                if line.startswith("STRIPE_WEBHOOK_SECRET="):
                    self.assertEqual(line.strip(), "STRIPE_WEBHOOK_SECRET=")


class ShutdownTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dw-shutdown-")
        self.db = os.path.join(self.tmp, "dw.sqlite3")

    def test_start_then_shutdown_joins_every_thread(self):
        cfg = config.load({"DROPWATCH_DB": self.db, "HOST": "127.0.0.1",
                           "PORT": "1", "DROPWATCH_RUNNER_INTERVAL": "0.05"})
        cfg["PORT"] = 0  # ephemeral; config itself requires 1-65535
        httpd, stop, threads = serve.start(cfg)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        url = "http://127.0.0.1:%d/api/status" % httpd.server_address[1]
        with urllib.request.urlopen(url, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
        started = time.monotonic()
        self.assertTrue(serve.shutdown(httpd, stop, threads, timeout=10))
        self.assertLess(time.monotonic() - started, 5)
        t.join(2)
        self.assertFalse(t.is_alive())

    def test_sigterm_exits_zero_and_logs_a_clean_stop(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        proc = subprocess.Popen(
            [sys.executable, "-m", "app.serve"], cwd=ROOT,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=dict(os.environ, DROPWATCH_DB=self.db, HOST="127.0.0.1",
                     PORT=str(port), DROPWATCH_RUNNER_INTERVAL="0.2"))
        try:
            deadline = time.monotonic() + 20
            while True:
                try:
                    urllib.request.urlopen(
                        "http://127.0.0.1:%d/api/status" % port, timeout=1).close()
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(0.1)
            proc.send_signal(signal.SIGTERM)
            _, err = proc.communicate(timeout=20)
        finally:
            if proc.poll() is None:
                proc.kill()
        self.assertEqual(proc.returncode, 0, err)
        self.assertIn("[RUNNER] serve stopping signal=True", err)
        self.assertIn("[RUNNER] serve stopped clean=True", err)


if __name__ == "__main__":
    unittest.main()
