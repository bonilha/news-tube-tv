"""Extract channels from x-live database and print them as SQL inserts."""
import sqlite3

db = sqlite3.connect(r"C:\Code\x-live\data\newstube.db")
db.row_factory = sqlite3.Row

tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
print("TABLES:", tables)

if "channels" in tables:
    cols = [r[1] for r in db.execute("PRAGMA table_info(channels)").fetchall()]
    print("\nCHANNELS columns:", cols)
    rows = db.execute("SELECT * FROM channels").fetchall()
    print(f"\nFound {len(rows)} channels:")
    for row in rows:
        d = dict(row)
        print(d)

db.close()
