"""deploy.backup -- nightly online backup of the Dropwatch sqlite file,
local retention, optional upload to an S3-compatible bucket (Backblaze
B2, AWS S3, ...), and restore. stdlib only.

    python -m deploy.backup run              back up now, prune, upload
    python -m deploy.backup restore FILE     replace the live db with FILE
    python -m deploy.backup verify FILE      integrity-check a backup

`run` uses sqlite's online backup API (what the sqlite3 shell's .backup
does), so it is safe while the app is writing. Each backup is
integrity-checked, gzipped, and written atomically as
dropwatch-<UTC timestamp>.sqlite3.gz; files older than BACKUP_KEEP_DAYS
are deleted. Upload happens only when all four BACKUP_S3_* credentials
are present.

`restore` must run with the app stopped (see deploy/README.md). It
checks the backup first, moves the current db and its -wal/-shm aside
as *.pre-restore-<ts> (never deletes them), then writes the backup into
place.

Environment:
    DROPWATCH_DB                  live db (default /data/dropwatch.sqlite3)
    BACKUP_DIR                    local backups (default /backups)
    BACKUP_KEEP_DAYS              local retention (default 14)
    BACKUP_S3_ENDPOINT            e.g. https://s3.us-west-004.backblazeb2.com
    BACKUP_S3_REGION              e.g. us-west-004
    BACKUP_S3_BUCKET
    BACKUP_S3_PREFIX              key prefix (default dropwatch/)
    BACKUP_S3_ACCESS_KEY_ID       SECRET-adjacent: scope the key to one bucket
    BACKUP_S3_SECRET_ACCESS_KEY   SECRET

Log lines are tagged [BACKUP].
"""

import datetime as dt
import gzip
import hashlib
import hmac
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

PREFIX = "dropwatch-"
SUFFIX = ".sqlite3.gz"
S3_VARS = ("BACKUP_S3_ENDPOINT", "BACKUP_S3_REGION", "BACKUP_S3_BUCKET",
           "BACKUP_S3_ACCESS_KEY_ID", "BACKUP_S3_SECRET_ACCESS_KEY")


class BackupError(Exception):
    pass


def log(msg, **fields):
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    extra = " ".join("%s=%s" % kv for kv in fields.items())
    print(("%s [BACKUP] %s %s" % (now, msg, extra)).rstrip(), file=sys.stderr,
          flush=True)


# ------------------------------------------------------------ snapshot --

def _integrity_ok(path):
    conn = sqlite3.connect("file:%s?mode=ro" % urllib.parse.quote(path),
                           uri=True)
    try:
        return conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def snapshot(db_path, dest_dir, now=None):
    """Online backup of db_path into dest_dir; returns the .gz path."""
    if not os.path.exists(db_path):
        raise BackupError("no database at %s" % db_path)
    now = now or dt.datetime.now(dt.timezone.utc)
    os.makedirs(dest_dir, exist_ok=True)
    if not os.access(dest_dir, os.W_OK):
        raise BackupError("%s is not writable by uid %d" % (dest_dir, os.getuid()))
    name = PREFIX + now.strftime("%Y%m%dT%H%M%SZ") + SUFFIX
    final = os.path.join(dest_dir, name)

    with tempfile.TemporaryDirectory(dir=dest_dir, prefix=".tmp-") as tmp:
        raw = os.path.join(tmp, "db.sqlite3")
        src = sqlite3.connect(db_path, timeout=30)
        dst = sqlite3.connect(raw)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        if not _integrity_ok(raw):
            raise BackupError("integrity_check failed on the fresh copy")
        part = final + ".part"
        with open(raw, "rb") as fin, gzip.open(part, "wb", compresslevel=6) as fout:
            shutil.copyfileobj(fin, fout)
        os.replace(part, final)
    return final


def prune(dest_dir, keep_days, now=None):
    """Delete our own backups older than keep_days. Returns names removed."""
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=keep_days)
    removed = []
    for name in sorted(os.listdir(dest_dir)):
        if not (name.startswith(PREFIX) and name.endswith(SUFFIX)):
            continue
        stamp = name[len(PREFIX):-len(SUFFIX)]
        try:
            when = dt.datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(
                tzinfo=dt.timezone.utc)
        except ValueError:
            continue
        if when < cutoff:
            os.remove(os.path.join(dest_dir, name))
            removed.append(name)
    return removed


# ------------------------------------------------------------- restore --

def _gunzip_to(path, out_path):
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rb") as fin, open(out_path, "wb") as fout:
        shutil.copyfileobj(fin, fout)


def verify(backup_path):
    with tempfile.TemporaryDirectory() as tmp:
        raw = os.path.join(tmp, "check.sqlite3")
        _gunzip_to(backup_path, raw)
        return _integrity_ok(raw)


def restore(backup_path, db_path, now=None):
    """Replace db_path with backup_path. The app must be stopped.
    Returns the list of files moved aside."""
    now = now or dt.datetime.now(dt.timezone.utc)
    db_dir = os.path.dirname(os.path.abspath(db_path))
    with tempfile.TemporaryDirectory(dir=db_dir, prefix=".restore-") as tmp:
        raw = os.path.join(tmp, "restore.sqlite3")
        _gunzip_to(backup_path, raw)
        if not _integrity_ok(raw):
            raise BackupError("backup failed integrity_check; live db untouched")
        moved = []
        tag = now.strftime("%Y%m%dT%H%M%SZ")
        # a stale -wal next to the restored file would be replayed onto it
        for suffix in ("", "-wal", "-shm"):
            path = db_path + suffix
            if os.path.exists(path):
                aside = "%s.pre-restore-%s%s" % (db_path, tag, suffix)
                os.replace(path, aside)
                moved.append(aside)
        os.replace(raw, db_path)
    return moved


# ---------------------------------------------------------- S3 upload --

def _sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def _hmac(key, msg):
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def sigv4_headers(method, url, region, access_key, secret_key, payload_hash,
                  amz_date, extra_headers=None, service="s3"):
    """AWS Signature Version 4 for one request. Returns the headers to send
    (Host, x-amz-date, x-amz-content-sha256, Authorization, + extras)."""
    u = urllib.parse.urlsplit(url)
    headers = {"host": u.netloc, "x-amz-date": amz_date,
               "x-amz-content-sha256": payload_hash}
    for k, v in (extra_headers or {}).items():
        headers[k.lower()] = v.strip()
    signed = sorted(headers)
    canonical_query = "&".join(
        "%s=%s" % (urllib.parse.quote(k, safe="-_.~"),
                   urllib.parse.quote(v, safe="-_.~"))
        for k, v in sorted(urllib.parse.parse_qsl(u.query, keep_blank_values=True)))
    canonical = "\n".join([
        method,
        urllib.parse.quote(urllib.parse.unquote(u.path or "/"), safe="/-_.~"),
        canonical_query,
        "".join("%s:%s\n" % (k, headers[k]) for k in signed),
        ";".join(signed),
        payload_hash,
    ])
    day = amz_date[:8]
    scope = "%s/%s/%s/aws4_request" % (day, region, service)
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope,
                         _sha256_hex(canonical.encode())])
    key = _hmac(("AWS4" + secret_key).encode(), day)
    for part in (region, service, "aws4_request"):
        key = _hmac(key, part)
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    out = {k: v for k, v in headers.items() if k != "host"}
    out["Authorization"] = (
        "AWS4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s"
        % (access_key, scope, ";".join(signed), signature))
    return out


def s3_config(env):
    """The S3 settings when every credential is present, else None."""
    if not all((env.get(k) or "").strip() for k in S3_VARS):
        return None
    cfg = {k: env[k].strip() for k in S3_VARS}
    cfg["BACKUP_S3_PREFIX"] = env.get("BACKUP_S3_PREFIX", "dropwatch/")
    return cfg


def upload(path, cfg, now=None, opener=urllib.request.urlopen):
    """PUT path to the bucket (path-style URL). Returns the object key."""
    now = now or dt.datetime.now(dt.timezone.utc)
    with open(path, "rb") as fh:
        body = fh.read()
    key = cfg["BACKUP_S3_PREFIX"] + os.path.basename(path)
    url = "%s/%s/%s" % (cfg["BACKUP_S3_ENDPOINT"].rstrip("/"),
                        cfg["BACKUP_S3_BUCKET"], urllib.parse.quote(key))
    headers = sigv4_headers(
        "PUT", url, cfg["BACKUP_S3_REGION"], cfg["BACKUP_S3_ACCESS_KEY_ID"],
        cfg["BACKUP_S3_SECRET_ACCESS_KEY"], _sha256_hex(body),
        now.strftime("%Y%m%dT%H%M%SZ"),
        {"content-type": "application/gzip"})
    req = urllib.request.Request(url, data=body, headers=headers, method="PUT")
    try:
        with opener(req, timeout=60) as resp:
            if resp.status // 100 != 2:
                raise BackupError("upload got HTTP %s" % resp.status)
    except urllib.error.HTTPError as exc:
        # never echo the response body: it can quote the request headers
        raise BackupError("upload got HTTP %s" % exc.code) from None
    return key


# ----------------------------------------------------------------- CLI --

def run(env):
    db_path = env.get("DROPWATCH_DB") or "/data/dropwatch.sqlite3"
    dest = env.get("BACKUP_DIR") or "/backups"
    keep = int(env.get("BACKUP_KEEP_DAYS") or 14)
    started = time.monotonic()
    path = snapshot(db_path, dest)
    removed = prune(dest, keep)
    log("snapshot written", file=os.path.basename(path),
        bytes=os.path.getsize(path), pruned=len(removed),
        ms=round((time.monotonic() - started) * 1000))
    cfg = s3_config(env)
    if cfg is None:
        log("upload skipped", reason="BACKUP_S3_* not set")
    else:
        key = upload(path, cfg)
        log("uploaded", bucket=cfg["BACKUP_S3_BUCKET"], key=key)
    return path


def main(argv=None, env=None):
    argv = sys.argv[1:] if argv is None else argv
    env = os.environ if env is None else env
    try:
        if argv == ["run"]:
            run(env)
        elif len(argv) == 2 and argv[0] == "verify":
            ok = verify(argv[1])
            log("verify", file=os.path.basename(argv[1]), ok=ok)
            return 0 if ok else 1
        elif len(argv) == 2 and argv[0] == "restore":
            db_path = env.get("DROPWATCH_DB") or "/data/dropwatch.sqlite3"
            moved = restore(argv[1], db_path)
            log("restored", file=os.path.basename(argv[1]),
                moved_aside=",".join(os.path.basename(m) for m in moved) or "-")
        else:
            print(__doc__.split("\n\n")[1], file=sys.stderr)
            return 2
    except (BackupError, OSError, sqlite3.Error) as exc:
        log("FAILED", error=type(exc).__name__, detail=str(exc)[:200])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
