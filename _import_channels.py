"""Extract channels from x-live and add to news-tube-tv."""
import sqlite3, os, time

# Try reading from the copied db first, then original
db_path = r"C:\Code\news-tube-tv\_xlive.db"
if not os.path.exists(db_path) or os.path.getsize(db_path) == 0:
    db_path = r"C:\Code\x-live\data\newstube.db"

print(f"Reading from: {db_path}")
print(f"Exists: {os.path.exists(db_path)}, Size: {os.path.getsize(db_path) if os.path.exists(db_path) else 0}")

try:
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    
    tables = [r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]
    print(f"Tables: {tables}")
    
    channels = []
    if "channels" in tables:
        cols = [r[1] for r in db.execute("PRAGMA table_info(channels)").fetchall()]
        print(f"Channels columns: {cols}")
        rows = db.execute("SELECT * FROM channels").fetchall()
        print(f"Found {len(rows)} channels")
        for row in rows:
            d = dict(row)
            print(f"  -> {d}")
            channels.append(d)
    db.close()
    
    # Now insert into news-tube-tv
    if channels:
        target = sqlite3.connect(r"C:\Code\news-tube-tv\data.db")
        target.row_factory = sqlite3.Row
        existing = {r[0] for r in target.execute("SELECT channel_id FROM channels").fetchall()}
        added = 0
        for ch in channels:
            cid = ch.get("ucid") or ch.get("channel_id") or ""
            if cid in existing:
                print(f"  SKIP (already exists): {ch.get('nome', ch.get('name', '?'))} ({cid})")
                continue
            handle = ch.get("handle", "")
            name = ch.get("nome") or ch.get("name") or handle or cid
            min_age = ch.get("min_age_hours", 2.0)
            target.execute(
                "INSERT OR IGNORE INTO channels (name, channel_id, handle, min_age_hours, active) VALUES (?, ?, ?, ?, ?)",
                (name, cid, handle, min_age, 1),
            )
            added += 1
            print(f"  ADDED: {name} ({cid})")
        target.commit()
        print(f"\nDone: {added} channels added, {len(channels) - added} skipped")
        target.close()
    else:
        print("No channels found in x-live database")
        
except Exception as e:
    print(f"ERROR: {e}")
    import traceback
    traceback.print_exc()
