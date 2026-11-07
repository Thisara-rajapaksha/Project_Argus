"""Labelled areas (door, hallway, ...) and the history of who was in which area.

Positions stay raw in the `positions` table. `rebuild()` combines them with the current areas and rewrites
both `zone_events` AND `positions.zone`, so after you redraw, rename or re-prioritise an area you can rebuild
the whole history (`rebuild_all()` does every run).

Where areas overlap, ONE rule decides everywhere (engine, rebuild, API, MCP server):
higher priority first, then the smaller polygon, then the lower id. See `order_key()`.
"""
import json
import sqlite3
import threading
import time

TYPES = ("door", "hallway", "room", "counter", "restricted", "other")
MIN_STAY_S = 0.5       # touching an area for less than this does not count as being in it
STEP_S = 0.2           # time between saved positions (5 per second)

SCHEMA = """
CREATE TABLE IF NOT EXISTS areas (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, type TEXT, note TEXT,
    priority INTEGER DEFAULT 0,          -- where areas overlap, the higher number wins
    anchor TEXT DEFAULT 'feet',          -- 'feet' = bottom of the box, 'center' = middle (use behind counters)
    polygon TEXT, created REAL);         -- JSON [[x, y], ...] with x, y in 0..1
CREATE TABLE IF NOT EXISTS zone_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, person INTEGER,
    area_id INTEGER, area_name TEXT, area_type TEXT,
    enter_t REAL, leave_t REAL, enter_ts TEXT, leave_ts TEXT, seconds REAL);
CREATE INDEX IF NOT EXISTS idx_ze ON zone_events (run_id, person, enter_t);
"""

_lock = threading.RLock()      # one rebuild at a time


def _conn(path):
    c = sqlite3.connect(path, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def init_areas(path):
    with _conn(path) as c:
        c.executescript(SCHEMA)


def _area(r):
    d = dict(r)
    d["polygon"] = json.loads(d["polygon"])
    return d


def list_areas(path):
    with _conn(path) as c:
        return [_area(r) for r in c.execute("SELECT * FROM areas ORDER BY id")]


def _clean(body, need_polygon):
    name = str(body.get("name", "")).strip()[:60]
    if not name:
        raise ValueError("name is required")
    typ = body.get("type", "other")
    if typ not in TYPES:
        raise ValueError(f"type must be one of {list(TYPES)}")
    anchor = body.get("anchor", "feet")
    if anchor not in ("feet", "center"):
        raise ValueError("anchor must be feet or center")
    try:
        priority = int(body.get("priority", 0))
    except (TypeError, ValueError):
        raise ValueError("priority must be a number")
    out = {"name": name, "type": typ, "note": str(body.get("note", ""))[:500], "priority": priority, "anchor": anchor}
    if need_polygon:
        try:
            poly = [[min(1.0, max(0.0, float(x))), min(1.0, max(0.0, float(y)))] for x, y in body.get("polygon", [])]
        except (TypeError, ValueError):
            raise ValueError("bad polygon")
        if not 3 <= len(poly) <= 200:
            raise ValueError("a polygon needs 3 to 200 points")
        out["polygon"] = json.dumps(poly)
    return out


def add_area(path, body):
    a = _clean(body, True)
    with _conn(path) as c:
        cur = c.execute("INSERT INTO areas (name, type, note, priority, anchor, polygon, created) VALUES (?,?,?,?,?,?,?)",
                        (a["name"], a["type"], a["note"], a["priority"], a["anchor"], a["polygon"], time.time()))
        return _area(c.execute("SELECT * FROM areas WHERE id=?", (cur.lastrowid,)).fetchone())


def update_area(path, aid, body):
    a = _clean(body, False)
    with _conn(path) as c:
        c.execute("UPDATE areas SET name=?, type=?, note=?, priority=?, anchor=? WHERE id=?",
                  (a["name"], a["type"], a["note"], a["priority"], a["anchor"], aid))
        r = c.execute("SELECT * FROM areas WHERE id=?", (aid,)).fetchone()
        return _area(r) if r else None


def delete_area(path, aid):
    """Delete an area and make sure nothing keeps pointing at it (no stale zone ids)."""
    with _conn(path) as c:
        c.execute("DELETE FROM areas WHERE id=?", (aid,))
        c.execute("UPDATE positions SET zone=NULL WHERE zone=?", (aid,))
        c.execute("DELETE FROM zone_events WHERE area_id=?", (aid,))


def _inside(p, poly):
    x, y, c = p[0], p[1], False
    for i in range(len(poly)):
        (x1, y1), (x2, y2) = poly[i], poly[i - 1]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            c = not c
    return c


def _size(poly):
    return abs(sum(poly[i - 1][0] * poly[i][1] - poly[i][0] * poly[i - 1][1] for i in range(len(poly)))) / 2


def order_key(a):
    """Sort key for overlapping areas: the smallest key wins. Priority, then smaller polygon, then lower id."""
    return (-(a.get("priority") or 0), _size(a["polygon"]), a["id"])


def rebuild(path, run_id=None, min_stay=MIN_STAY_S):
    """Rewrite zone_events and positions.zone for one run (default: the latest). Returns how many area visits were saved."""
    with _lock, _conn(path) as c:
        if run_id is None:
            run_id = c.execute("SELECT MAX(id) FROM runs").fetchone()[0]
        if run_id is None:
            return 0
        areas = sorted((_area(r) for r in c.execute("SELECT * FROM areas")), key=order_key)   # winner first
        by_id = {a["id"]: a for a in areas}
        c.execute("DELETE FROM zone_events WHERE run_id=?", (run_id,))
        per = {}
        for r in c.execute("SELECT id, person, t, ts, x, y, x1, y1, x2, y2, zone FROM positions WHERE run_id=? "
                           "ORDER BY person, frame", (run_id,)):
            per.setdefault(r["person"], []).append(r)
        saved, zone_updates = 0, []
        for person, rows in per.items():
            segs = []                                   # [area_id or None, t_first, t_last, ts_first, ts_last]
            for r in rows:
                aid = None
                for a in areas:
                    pt = (r["x"], r["y"]) if a["anchor"] == "feet" else ((r["x1"] + r["x2"]) / 2, (r["y1"] + r["y2"]) / 2)
                    if _inside(pt, a["polygon"]):
                        aid = a["id"]
                        break
                if aid != r["zone"]:
                    zone_updates.append((aid, r["id"]))
                if segs and segs[-1][0] == aid:
                    segs[-1][2], segs[-1][4] = r["t"], r["ts"]
                else:
                    segs.append([aid, r["t"], r["t"], r["ts"], r["ts"]])
            segs = [s for s in segs if s[2] - s[1] + STEP_S >= min_stay]      # drop border flicker
            merged = []
            for s in segs:                                                    # A, (flicker), A -> one visit
                if merged and merged[-1][0] == s[0]:
                    merged[-1][2], merged[-1][4] = s[2], s[4]
                else:
                    merged.append(s)
            for aid, t0, t1, ts0, ts1 in merged:
                if aid is None:
                    continue
                a = by_id[aid]
                c.execute("INSERT INTO zone_events (run_id, person, area_id, area_name, area_type, enter_t, leave_t, "
                          "enter_ts, leave_ts, seconds) VALUES (?,?,?,?,?,?,?,?,?,?)",
                          (run_id, person, aid, a["name"], a["type"], t0, t1, ts0, ts1, round(t1 - t0 + STEP_S, 2)))
                saved += 1
        c.executemany("UPDATE positions SET zone=? WHERE id=?", zone_updates)
        return saved


def rebuild_all(path):
    """Rebuild every run. Use after areas change, and once after upgrading an old database."""
    with _lock:
        with _conn(path) as c:
            ids = [r[0] for r in c.execute("SELECT id FROM runs ORDER BY id")]
        return sum(rebuild(path, rid) for rid in ids)


def list_events(path, run_id=None, limit=200):
    with _conn(path) as c:
        if run_id is None:
            run_id = c.execute("SELECT MAX(id) FROM runs").fetchone()[0]
        return [dict(r) for r in c.execute(
            "SELECT * FROM zone_events WHERE run_id=? ORDER BY enter_t LIMIT ?", (run_id, limit))]
