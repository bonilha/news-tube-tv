"""Read x-live database channels."""
import sqlite3, os, sys

paths = [
    r"C:\Code\x-live\data\newstube.db",
    r"C:\Code\news-tube-tv\_xlive.db",
]

for p in paths:
    print(f"\n=== {p} ===")
    print(f"  exists: {os.path.exists(p)}")
    if os.path.exists(p):
        print(f"  size: {os.path.getsize(p)}")
        try:
            db = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
            db.row_factory = sqlite3.Row
            tables = [r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()]
            print(f"  tables: {tables}")
            if "channels" in tables:
                cols = [r[1] for r in db.execute(
                    "PRAGMA table_info(channels)"
                ).fetchall()]
                print(f"  channels columns: {cols}")
                rows = db.execute("SELECT * FROM channels").fetchall()
                print(f"  channels count: {len(rows)}")
                for row in rows:
                    print(f"  -> {dict(row)}")
            db.close()
        except Exception as e:
            print(f"  ERROR: {e}")

# Also try listing the data dir
data_dir = r"C:\Code\x-live\data"
if os.path.isdir(data_dir):
    print(f"\n=== data dir contents ===")
    for f in os.listdir(data_dir):
        full = os.path.join(data_dir, f)
        print(f"  {f} ({os.path.getsize(full) if os.path.isfile(full) else 'dir'})")
