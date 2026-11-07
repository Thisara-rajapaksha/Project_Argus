"""One-time upgrade of guard.db for the Argus MCP server. Safe to run again.

    python migrate.py [path\\to\\guard.db]        (stop the backend first)

What it does:
  1. makes a backup copy next to the database (guard.db.bak-<time>)
  2. adds events.video_ts and positions.zone if missing
  3. creates the proposals and mcp_audit tables
  4. recomputes positions.zone and zone_events for EVERY run, so deleted areas disappear and new ones are filled in
"""
import os
import sqlite3
import sys
import time

import areas
import proposals

HERE = os.path.dirname(os.path.abspath(__file__))
path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "guard.db")
if not os.path.exists(path):
    raise SystemExit(f"No database at {path}")

backup = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
src, dst = sqlite3.connect(path), sqlite3.connect(backup)
src.backup(dst)                                   # safe even while the file is in WAL mode
dst.close()
src.close()
print("backup:", backup)

c = sqlite3.connect(path)
tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
cols = lambda t: [r[1] for r in c.execute(f"PRAGMA table_info({t})")]
if "events" in tables and "video_ts" not in cols("events"):
    c.execute("ALTER TABLE events ADD COLUMN video_ts TEXT")
    print("added events.video_ts")
if "positions" in tables and "zone" not in cols("positions"):
    c.execute("ALTER TABLE positions ADD COLUMN zone INTEGER")
    print("added positions.zone")
c.commit()
c.close()

proposals.init_proposals(path)
areas.init_areas(path)
print("proposals and mcp_audit tables ready")

if "positions" in tables and "runs" in tables:
    c = sqlite3.connect(path)
    before = c.execute("SELECT SUM(zone IS NOT NULL), COUNT(*) FROM positions").fetchone()
    c.close()
    visits = areas.rebuild_all(path)
    c = sqlite3.connect(path)
    after = c.execute("SELECT SUM(zone IS NOT NULL), COUNT(*) FROM positions").fetchone()
    names = [r[0] for r in c.execute("SELECT name FROM areas")]
    c.close()
    print(f"zones: {before[0] or 0} of {before[1]} positions were inside an area, now {after[0] or 0}  "
          f"({visits} area visits rebuilt; areas: {names or 'none drawn yet'})")
else:
    print("no positions yet: run the backend once, then run this again")
print("done")
