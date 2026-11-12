"""The app of the python benchmark image: stdlib only. Imports what an ordinary service imports, answers a few
random-access queries over data.db (random reads across the file's blobs), then serves GET / with a known body."""
import time

STARTED = time.monotonic()

import argparse
import asyncio
import csv
import datetime
import decimal
import email.message
import email.utils
import hashlib
import http.server
import io
import json
import logging
import os
import random
import re
import sqlite3
import ssl
import urllib.parse
import uuid
import zipfile

IMPORTED = time.monotonic()
BODY = b"bench-python ok\n"
ROWS = 4000
QUERIES = 64


async def warm():
    async def one(n):
        await asyncio.sleep(0)
        return decimal.Decimal(n) / decimal.Decimal(7)

    return await asyncio.gather(*(one(n) for n in range(1, 50)))


def queries(path):
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    count = db.execute("SELECT count(*) FROM items").fetchone()[0]
    if count != ROWS:
        raise SystemExit(f"data.db has {count} rows, not {ROWS}")
    rng = random.Random(42)
    hasher = hashlib.sha256()
    for _ in range(QUERIES):
        row = db.execute("SELECT k, payload FROM items WHERE id = ?", (rng.randint(1, ROWS),)).fetchone()
        hasher.update(row[1])
        if db.execute("SELECT id FROM items WHERE k = ?", (row[0],)).fetchone() is None:
            raise SystemExit("the index does not find a key")
    db.close()
    return hasher.hexdigest()


def modules():
    msg = email.message.EmailMessage()
    msg["Date"] = email.utils.format_datetime(datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc))
    msg.set_content("x")
    out = io.StringIO()
    csv.writer(out).writerow([str(uuid.UUID(int=1)), json.dumps({"a": 1}), re.sub("a", "b", "aaa")])
    zipfile.ZipFile(io.BytesIO(), "w").close()
    argparse.ArgumentParser().parse_args([])
    return urllib.parse.quote(msg["Date"]), ssl.OPENSSL_VERSION


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(BODY)))
        self.end_headers()
        self.wfile.write(BODY)

    def log_message(self, *args):
        pass


def main():
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(warm())
    modules()
    warmed = time.monotonic()
    checksum = queries(os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.db"))
    done = time.monotonic()
    print(
        f"python app: imports {IMPORTED - STARTED:.1f}s, warm-up {warmed - IMPORTED:.1f}s, {QUERIES} queries "
        f"{done - warmed:.1f}s ({checksum[:12]}), listening",
        flush=True,
    )
    http.server.ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), Handler).serve_forever()


main()
