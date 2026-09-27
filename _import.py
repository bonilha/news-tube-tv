"""Import channels from x-live database into news-tube-tv."""
import sqlite3

XLIVE = r"C:\Code\news-tube-tv\_xlive.db"
NTTV = r"C:\Code\news-tube-tv\data.db"

# Read channels from x-live
src = sqlite3.connect(XLIVE)
src.row_factory = sqlite3.Row
rows = src.execute("SELECT nome, handle, ucid FROM channels").fetchall()
src.close()

# Insert into news-tube-tv
dst = sqlite3.connect(NTTV)
existing = {r[0] for r in dst.execute("SELECT channel_id FROM channels").fetchall()}
added = 0
for ch in rows:
    cid = ch["ucid"]
    if cid in existing:
        print(f"  SKIP (exists): {ch['nome']} ({cid})")
        continue
    dst.execute(
        "INSERT INTO channels (name, channel_id, handle, min_age_hours, active) VALUES (?, ?, ?, 2.0, 1)",
        (ch["nome"], cid, ch["handle"]),
    )
    added += 1
    print(f"  ADDED: {ch['nome']} ({cid}) {ch['handle']}")
dst.commit()
dst.close()
print(f"\n{added} canais importados")
