"""deploy.backup -- snapshot under a live writer, retention, the restore
round trip on a real engine database, and S3 SigV4 signing (AWS's own
published example plus a local fake bucket). No real network."""

import datetime as dt
import gzip
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from deploy import backup  # noqa: E402
from engine import core, db  # noqa: E402

T0 = 1_700_000_000.0
NOW = dt.datetime(2026, 10, 27, 3, 17, 0, tzinfo=dt.timezone.utc)


def _live_db(path, n_subs=3):
    conn = db.connect(path)
    core.register_drop(conn, "rg-1", "Camp", risk_tier="green", now=T0)
    for i in range(n_subs):
        row = core.subscribe(conn, "rg-1", "ntfy", "topic-%d" % i, "free", T0)
        core.verify(conn, row["id"], row["verify_code"], T0)
    return conn


def _count_subs(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0]
    finally:
        conn.close()


class SnapshotRestoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dw-backup-")
        self.db_path = os.path.join(self.tmp, "data", "dropwatch.sqlite3")
        os.makedirs(os.path.dirname(self.db_path))
        self.backups = os.path.join(self.tmp, "backups")

    def test_snapshot_while_writing_then_restore_round_trip(self):
        conn = _live_db(self.db_path)
        # an open write transaction on the live db (WAL) must not block
        # or corrupt the online backup
        conn.execute("BEGIN")
        conn.execute("UPDATE drops SET title = 'uncommitted' WHERE id = 'rg-1'")
        path = backup.snapshot(self.db_path, self.backups, now=NOW)
        conn.rollback()
        self.assertEqual(os.path.basename(path),
                         "dropwatch-20261027T031700Z.sqlite3.gz")
        self.assertTrue(backup.verify(path))

        # life goes on after the backup: more rows, then disaster
        row = core.subscribe(conn, "rg-1", "ntfy", "late", "free", T0)
        core.verify(conn, row["id"], row["verify_code"], T0)
        conn.close()
        self.assertEqual(_count_subs(self.db_path), 4)

        moved = backup.restore(path, self.db_path, now=NOW)
        self.assertIn(self.db_path + ".pre-restore-20261027T031700Z", moved)
        self.assertEqual(_count_subs(self.db_path), 3)
        # the restored file opens through the engine with schema intact
        conn = db.connect(self.db_path)
        self.assertEqual(core.get_drop(conn, "rg-1")["title"], "Camp")
        self.assertEqual(len(core.active_watches(conn, dt.date(2026, 1, 1))), 0)
        conn.close()

    def test_restore_refuses_a_corrupt_backup_and_leaves_live_db_alone(self):
        _live_db(self.db_path).close()
        bad = os.path.join(self.tmp, "bad.sqlite3.gz")
        with gzip.open(bad, "wb") as fh:
            fh.write(b"SQLite format 3\x00" + b"\x00" * 200)
        with self.assertRaises((backup.BackupError, sqlite3.DatabaseError)):
            backup.restore(bad, self.db_path)
        self.assertEqual(_count_subs(self.db_path), 3)
        self.assertEqual([n for n in os.listdir(os.path.dirname(self.db_path))
                          if "pre-restore" in n], [])

    def test_missing_db_is_an_error_not_an_empty_backup(self):
        with self.assertRaises(backup.BackupError):
            backup.snapshot(self.db_path, self.backups, now=NOW)

    def test_prune_keeps_recent_and_foreign_files(self):
        os.makedirs(self.backups)
        names = ["dropwatch-20261001T031700Z.sqlite3.gz",   # 26 days: gone
                 "dropwatch-20261020T031700Z.sqlite3.gz",   # 7 days: kept
                 "notes.txt", "dropwatch-garbage.sqlite3.gz"]
        for n in names:
            open(os.path.join(self.backups, n), "w").close()
        removed = backup.prune(self.backups, keep_days=14, now=NOW)
        self.assertEqual(removed, ["dropwatch-20261001T031700Z.sqlite3.gz"])
        self.assertEqual(sorted(os.listdir(self.backups)), sorted(names[1:]))

    def test_cli_run_without_s3_creds_backs_up_locally(self):
        _live_db(self.db_path).close()
        env = {"DROPWATCH_DB": self.db_path, "BACKUP_DIR": self.backups}
        self.assertEqual(backup.main(["run"], env), 0)
        files = os.listdir(self.backups)
        self.assertEqual(len(files), 1)
        self.assertEqual(backup.main(["verify", os.path.join(self.backups, files[0])], env), 0)
        self.assertEqual(backup.main(["restore", os.path.join(self.backups, files[0])], env), 0)
        self.assertEqual(_count_subs(self.db_path), 3)
        self.assertEqual(backup.main(["bogus"], env), 2)


class SigV4Test(unittest.TestCase):
    def test_aws_published_example(self):
        # "GET Object" example from AWS's S3 Signature Version 4 docs
        out = backup.sigv4_headers(
            "GET", "https://examplebucket.s3.amazonaws.com/test.txt",
            "us-east-1", "AKIAIOSFODNN7EXAMPLE",
            "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "20130524T000000Z", {"range": "bytes=0-9"})
        self.assertEqual(
            out["Authorization"],
            "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/"
            "us-east-1/s3/aws4_request, SignedHeaders=host;range;"
            "x-amz-content-sha256;x-amz-date, Signature="
            "f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41")

    def test_s3_config_needs_every_credential(self):
        full = {k: "x" for k in backup.S3_VARS}
        self.assertIsNotNone(backup.s3_config(full))
        for k in backup.S3_VARS:
            partial = dict(full, **{k: ""})
            self.assertIsNone(backup.s3_config(partial), k)


class _FakeBucket(BaseHTTPRequestHandler):
    received = []
    status = 200

    def do_PUT(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        type(self).received.append((self.path, dict(self.headers), body))
        self.send_response(type(self).status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


class UploadTest(unittest.TestCase):
    def setUp(self):
        _FakeBucket.received = []
        _FakeBucket.status = 200
        self.httpd = HTTPServer(("127.0.0.1", 0), _FakeBucket)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.cfg = {
            "BACKUP_S3_ENDPOINT": "http://127.0.0.1:%d" % self.httpd.server_address[1],
            "BACKUP_S3_REGION": "us-west-004", "BACKUP_S3_BUCKET": "dw-backups",
            "BACKUP_S3_ACCESS_KEY_ID": "AKID", "BACKUP_S3_SECRET_ACCESS_KEY": "s3cr3t",
            "BACKUP_S3_PREFIX": "dropwatch/"}
        self.file = os.path.join(tempfile.mkdtemp(), "dropwatch-20261027T031700Z.sqlite3.gz")
        with open(self.file, "wb") as fh:
            fh.write(b"payload")

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def test_signed_put_lands_at_bucket_key(self):
        key = backup.upload(self.file, self.cfg, now=NOW)
        self.assertEqual(key, "dropwatch/dropwatch-20261027T031700Z.sqlite3.gz")
        path, headers, body = _FakeBucket.received[0]
        self.assertEqual(path, "/dw-backups/" + key)
        self.assertEqual(body, b"payload")
        self.assertTrue(headers["Authorization"].startswith(
            "AWS4-HMAC-SHA256 Credential=AKID/20261027/us-west-004/s3/aws4_request"))
        self.assertNotIn("s3cr3t", json.dumps(headers))

    def test_http_error_is_reported_without_body(self):
        _FakeBucket.status = 403
        with self.assertRaises(backup.BackupError) as ctx:
            backup.upload(self.file, self.cfg, now=NOW)
        self.assertEqual(str(ctx.exception), "upload got HTTP 403")


if __name__ == "__main__":
    unittest.main()
