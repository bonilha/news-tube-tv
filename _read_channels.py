"""Read x-live database channels from workspace copy."""
import sqlite3, os

db_path = r"C:\Code\news-tube-tv\_xlive.db"
print(f"Exists: {os.path.exists(db_path)}, Size: {os.path.getsize(db_path) if os.path.exists(db_path) else 0}")

db = sqlite3.connect(db_path)
db.row_factory = sqlite3.Row

tables = [r[0] for r in db.execute(
    "SELECT name FROM sqlite_master WHERE type='table'"
).fetchall()]
print(f"Tables: {tables}")

if "channels" in tables:
    rows = db.execute("SELECT * FROM channels").fetchall()
    print(f"\n{len(rows)} channels:")
    for row in rows:
        print(dict(row))

db.close()
