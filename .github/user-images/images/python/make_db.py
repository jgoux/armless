"""Builds the SQLite file of the image at build time: 4000 rows of 1 KiB of pseudo-random (so incompressible, and
deterministic) bytes, about 4 MB, with an index on a text key. The app reads random rows of it before it listens."""
import random
import sqlite3
import sys

ROWS = 4000
rng = random.Random(1)
db = sqlite3.connect(sys.argv[1])
db.execute("PRAGMA page_size = 4096")
db.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, k TEXT NOT NULL, payload BLOB NOT NULL)")
db.executemany(
    "INSERT INTO items (id, k, payload) VALUES (?, ?, ?)",
    ((i, f"key-{rng.getrandbits(64):016x}", rng.randbytes(1024)) for i in range(1, ROWS + 1)),
)
db.execute("CREATE INDEX items_k ON items (k)")
db.commit()
db.execute("VACUUM")
db.close()
