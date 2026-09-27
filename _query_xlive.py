import sqlite3, json

db = sqlite3.connect(r"C:\Code\x-live\data\newstube.db")
db.row_factory = sqlite3.Row

tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
print("TABLES:", tables)

for t in tables:
    cols = [r[1] for r in db.execute(f"PRAGMA table_info({t})").fetchall()]
    print(f"\n--- {t} ---")
    print("COLUMNS:", cols)
    rows = db.execute(f"SELECT * FROM {t}").fetchall()
    for row in rows:
        print(dict(row))

db.close()
