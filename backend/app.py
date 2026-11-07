"""Argus backend: Flask + people tracking (YOLO + BoT-SORT + Relinker) + SQLite.

Run (Windows):  set SOURCE=C:\\path\\to\\video.mp4
                python app.py
Options (environment variables):
  START="2026-10-26 22:00:00"   virtual clock: the time the video starts (default: now)
  LOOP=1                        play the video again when it ends (each pass is a new run)
  REALTIME=1                    process at video speed instead of as fast as the GPU allows
  MODEL=yolo11m.pt  IMGSZ=1920
"""
import json
import os
import queue
import sqlite3
import time
from datetime import datetime, timedelta

from flask import Flask, Response, jsonify, request, send_from_directory

import areas as A
import rules as R
from engine import Engine, init_db

SOURCE = os.environ.get("SOURCE", r"C:\Users\LENOVO\Downloads\argus-dashboard-v2\test-\Screen Recording 2026-10-12 112521.mp4")
HERE = os.path.dirname(os.path.abspath(__file__))
MEDIA = os.path.join(HERE, "media")
DB = os.path.join(HERE, "guard.db")
CAMS = ["Camera 1"]
os.makedirs(MEDIA, exist_ok=True)

app = Flask(__name__)


state = {"armed": True, "online": False, "finished": False, "fps": 0.0, "alert": False,
         "alerts": [], "people": 0, "t": 0.0}
latest = {"jpeg": None}
subscribers = []


# ---------- database ----------
def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


init_db(DB)
A.init_areas(DB)
R.init_rules(DB)
with db() as c:
    c.execute("""CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, type TEXT, label TEXT,
        score REAL, snapshot TEXT, status TEXT DEFAULT 'new')""")
    if "cam" not in [r[1] for r in c.execute("PRAGMA table_info(events)")]:
        c.execute("ALTER TABLE events ADD COLUMN cam INTEGER DEFAULT 1")
    have = [r[1] for r in c.execute("PRAGMA table_info(events)")]
    for col, ddl in (("severity", "TEXT DEFAULT 'warning'"), ("rule_id", "INTEGER"), ("frame_snapshot", "TEXT")):
        if col not in have:
            c.execute(f"ALTER TABLE events ADD COLUMN {col} {ddl}")


def add_event(kind, label, score, jpeg, cam=1, severity="warning", rule_id=None, frame_jpeg=None):
    """jpeg = photo of the person, frame_jpeg = the whole camera frame at that moment."""
    ts = time.time()
    stamp = int(ts * 1000)
    name, frame_name = f"{stamp}.jpg", f"{stamp}_frame.jpg"
    if jpeg:
        with open(os.path.join(MEDIA, name), "wb") as f:
            f.write(jpeg)
    if frame_jpeg:
        with open(os.path.join(MEDIA, frame_name), "wb") as f:
            f.write(frame_jpeg)
    with db() as c:
        cur = c.execute("INSERT INTO events (ts, type, label, score, snapshot, cam, severity, rule_id, frame_snapshot) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (ts, kind, label, score, f"/media/{name}" if jpeg else None, cam, severity, rule_id,
                         f"/media/{frame_name}" if frame_jpeg else None))
        row = dict(c.execute("SELECT * FROM events WHERE id=?", (cur.lastrowid,)).fetchone())
    for q in list(subscribers):
        q.put(json.dumps(row))
    return row


def publish(jpeg):
    latest["jpeg"] = jpeg


def mjpeg():
    while True:
        data = latest["jpeg"]
        if data:
            yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + data + b"\r\n"
        time.sleep(0.04)


@app.before_request
def check_cam():
    """Every /api/cameras/<cid>/... route returns 404 for a camera that does not exist."""
    cid = (request.view_args or {}).get("cid")
    if cid is not None and not 1 <= cid <= len(CAMS):
        return jsonify(error="unknown camera"), 404


# ---------- cameras and video ----------
@app.get("/api/cameras")
def cameras():
    return jsonify([{"id": i + 1, "name": n, "online": state["online"], "armed": state["armed"]}
                    for i, n in enumerate(CAMS)])


@app.get("/api/cameras/<int:cid>/stream")
def stream(cid):
    return Response(mjpeg(), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.get("/api/cameras/<int:cid>/snapshot")
def snapshot(cid):
    return Response(latest["jpeg"] or b"", mimetype="image/jpeg")


# ---------- alerts ----------
@app.get("/api/events")
def events():
    limit = min(int(request.args.get("limit", 50)), 200)
    with db() as c:
        rows = c.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return jsonify([dict(r) for r in rows])


@app.post("/api/events/<int:eid>/ack")
def ack(eid):
    status = "false_alarm" if (request.get_json(silent=True) or {}).get("false_alarm") else "acknowledged"
    with db() as c:
        c.execute("UPDATE events SET status=? WHERE id=?", (status, eid))
    return jsonify(id=eid, status=status)


@app.get("/api/events/stream")
def events_stream():
    q = queue.Queue()
    subscribers.append(q)

    def gen():
        try:
            while True:
                try:
                    yield f"data: {q.get(timeout=15)}\n\n"
                except queue.Empty:
                    yield ": keep-alive\n\n"
        finally:
            subscribers.remove(q)

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.put("/api/arm")
def arm():
    state["armed"] = bool((request.get_json(silent=True) or {}).get("armed"))
    return jsonify(armed=state["armed"])


@app.get("/api/health")
def health():
    return jsonify(**{k: state[k] for k in ("online", "finished", "armed", "fps", "alert", "alerts", "people")})


# ---------- live people, computed from the raw positions ----------
MIN_SAMPLES = 5      # fewer samples than this (about 1 s) is flicker, not a person yet
GONE_AFTER = 1.5     # seconds without a sample -> the person has left


def inside(poly, x, y):
    """Ray casting: is the point (x, y) inside the polygon?"""
    ok, j = False, len(poly) - 1
    for i in range(len(poly)):
        (xi, yi), (xj, yj) = poly[i], poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            ok = not ok
        j = i
    return ok


def area_at(areas, p):
    """Name of the highest-priority labelled area containing this sample, or None."""
    cx, cy = (p["x1"] + p["x2"]) / 2, (p["y1"] + p["y2"]) / 2
    best = None
    for a in areas:
        x, y = (cx, cy) if a.get("anchor") == "center" else (p["x"], p["y"])
        if inside(a["polygon"], x, y) and (best is None or (a.get("priority") or 0) > (best.get("priority") or 0)):
            best = a
    return best["name"] if best else None


def photo_url(path):
    """Photo path with its file time added, so the browser reloads a photo the engine has improved."""
    if not path:
        return None
    try:
        return f"{path}?v={int(os.path.getmtime(os.path.join(MEDIA, path[len('/media/'):])))}"
    except OSError:
        return path


def dur(s):
    s = int(round(s))
    return f"{s // 60}m {s % 60}s" if s >= 60 else f"{s}s"


@app.get("/api/cameras/<int:cid>/people")
def people(cid):
    """Everyone tracked in the latest run, newest first, each with a one-line summary of the labelled areas they used.
    Unlabelled stretches are skipped. Recomputed on every call, so it is live and follows label edits."""
    with db() as c:
        run = c.execute("SELECT MAX(id) FROM runs").fetchone()[0]
        if run is None:
            return jsonify(run=None, people=[])
        now_t = state["t"]                               # current video second, set by the engine
        rows = c.execute("SELECT person, t, ts, x, y, x1, y1, x2, y2 FROM positions "
                         "WHERE run_id=? ORDER BY person, frame", (run,)).fetchall()
        snaps = {r["person"]: r["snapshot"] for r in
                 c.execute("SELECT person, snapshot FROM persons WHERE run_id=?", (run,))}
    playing = state["online"] and not state["finished"]
    areas = A.list_areas(DB)
    by = {}
    for r in rows:
        by.setdefault(r["person"], []).append(r)
    out = []
    for pid, pts in by.items():
        if len(pts) < MIN_SAMPLES:
            continue
        route = []                                   # [[area, seconds], ...]
        for i, p in enumerate(pts):
            name = area_at(areas, p)
            dt = min(pts[i + 1]["t"] - p["t"], 1.0) if i + 1 < len(pts) else 0
            if name is None:
                continue
            if route and route[-1][0] == name:
                route[-1][1] += dt
            else:
                route.append([name, dt])
        out.append({
            "person": pid, "snapshot": photo_url(snaps.get(pid)),
            "first_ts": pts[0]["ts"], "last_ts": pts[-1]["ts"],
            "seconds": round(pts[-1]["t"] - pts[0]["t"]),
            "in_view": playing and now_t - pts[-1]["t"] <= GONE_AFTER,
            "summary": " → ".join(f"{n} {dur(d)}" for n, d in route) or "Not in any labeled area",
        })
    out.sort(key=lambda p: p["first_ts"], reverse=True)
    return jsonify(run=run, people=out)


@app.get("/api/cameras/<int:cid>/people/<int:pid>/positions")
def person_positions(cid, pid):
    """One person's saved positions (x, y in 0..1) with the labelled area they were in at that moment."""
    with db() as c:
        run = c.execute("SELECT MAX(id) FROM runs").fetchone()[0]
        pos = c.execute("SELECT t, ts, x, y, x1, y1, x2, y2 FROM positions "
                        "WHERE run_id=? AND person=? ORDER BY frame", (run, pid)).fetchall()
    areas = A.list_areas(DB)
    return jsonify([{"t": p["t"], "ts": p["ts"], "x": round(p["x"], 4), "y": round(p["y"], 4),
                     "area": area_at(areas, p)} for p in pos])


# ---------- labelled areas (URLs are per camera; the table is shared until it gets a cam column) ----------
@app.get("/api/cameras/<int:cid>/areas")
def areas_list(cid):
    return jsonify(A.list_areas(DB))


@app.post("/api/cameras/<int:cid>/areas")
def areas_add(cid):
    """POST {name, type, note, priority, anchor, polygon: [[x, y], ...]} with x, y in 0..1."""
    try:
        return jsonify(A.add_area(DB, request.get_json(silent=True) or {})), 201
    except ValueError as e:
        return jsonify(error=str(e)), 400


@app.put("/api/cameras/<int:cid>/areas/<int:aid>")
def areas_update(cid, aid):
    try:
        row = A.update_area(DB, aid, request.get_json(silent=True) or {})
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return (jsonify(row), 200) if row else (jsonify(error="unknown area"), 404)


@app.delete("/api/cameras/<int:cid>/areas/<int:aid>")
def areas_delete(cid, aid):
    A.delete_area(DB, aid)
    R.delete_for_area(DB, aid)
    return jsonify(deleted=aid)


@app.post("/api/cameras/<int:cid>/areas/rebuild")
def areas_rebuild(cid):
    """Recompute who was in which area, from the raw positions, for the latest run."""
    return jsonify(events=A.rebuild(DB))


@app.get("/api/cameras/<int:cid>/areas/events")
def areas_events(cid):
    return jsonify(A.list_events(DB))


@app.get("/api/cameras/<int:cid>/positions")
def positions(cid):
    """Feet positions [x, y] (0..1) of the latest run, thinned to about 3000 points, for the path overlay."""
    with db() as c:
        run = c.execute("SELECT MAX(id) FROM runs").fetchone()[0]
        rows = c.execute("SELECT x, y FROM positions WHERE run_id=?", (run,)).fetchall()
    step = max(1, len(rows) // 3000)
    return jsonify([[round(r["x"], 4), round(r["y"], 4)] for r in rows[::step]])


# ---------- alert rules ----------
@app.get("/api/cameras/<int:cid>/rules")
def rules_list(cid):
    return jsonify(R.list_rules(DB, cid))


@app.post("/api/cameras/<int:cid>/rules")
def rules_add(cid):
    """POST {area_id|null, condition, param, severity, cooldown, schedule: {days, windows, outside}, enabled}."""
    try:
        return jsonify(R.add_rule(DB, request.get_json(silent=True) or {}, cid)), 201
    except ValueError as e:
        return jsonify(error=str(e)), 400


@app.put("/api/cameras/<int:cid>/rules/<int:rid>")
def rules_update(cid, rid):
    try:
        row = R.update_rule(DB, rid, request.get_json(silent=True) or {})
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return (jsonify(row), 200) if row else (jsonify(error="unknown rule"), 404)


@app.delete("/api/cameras/<int:cid>/rules/<int:rid>")
def rules_delete(cid, rid):
    R.delete_rule(DB, rid)
    return jsonify(deleted=rid)


# ---------- activity chart ----------
@app.get("/api/cameras/<int:cid>/activity")
def activity(cid):
    """People per time bucket, the most people at once per bucket, and where time was spent (latest run)."""
    with db() as c:
        run = c.execute("SELECT MAX(id) FROM runs").fetchone()[0]
        if run is None:
            return jsonify(empty=True)
        started = c.execute("SELECT started_at FROM runs WHERE id=?", (run,)).fetchone()[0]
        rows = c.execute("SELECT person, frame, t, x, y, x1, y1, x2, y2 FROM positions WHERE run_id=?", (run,)).fetchall()
    counts = {}
    for r in rows:
        counts[r["person"]] = counts.get(r["person"], 0) + 1
    rows = [r for r in rows if counts[r["person"]] >= MIN_SAMPLES]
    if not rows:
        return jsonify(empty=True)
    dur_s = max(r["t"] for r in rows)
    size = 10 if dur_s <= 300 else 60 if dur_s <= 7200 else 600 if dur_s <= 86400 else 3600
    start = datetime.fromisoformat(started)
    n = int(dur_s // size) + 1
    people_in = [set() for _ in range(n)]
    by_frame = {}
    for r in rows:
        b = int(r["t"] // size)
        people_in[b].add(r["person"])
        by_frame.setdefault(r["frame"], [b, set()])[1].add(r["person"])
    peak = [0] * n
    for b, s in by_frame.values():
        peak[b] = max(peak[b], len(s))
    fmt = "%H:%M:%S" if size < 60 else "%H:%M"
    buckets = [{"label": (start + timedelta(seconds=i * size)).strftime(fmt), "people": len(people_in[i]), "peak": peak[i]}
               for i in range(n)]
    areas = A.list_areas(DB)
    sample = rows[::max(1, len(rows) // 20000)]
    tally = {}
    for r in sample:
        name = area_at(areas, r)
        if name:
            tally[name] = tally.get(name, 0) + 1
    total = sum(tally.values())
    top_areas = [{"name": k, "share": round(v / total, 3)} for k, v in sorted(tally.items(), key=lambda kv: -kv[1])] if total else []
    best = max(range(n), key=lambda i: peak[i])
    return jsonify(empty=False, bucket_seconds=size, buckets=buckets, areas=top_areas, people=len({p for p, k in counts.items() if k >= MIN_SAMPLES}),
                   peak={"count": peak[best], "at": buckets[best]["label"]})


@app.post("/api/dev/fake-event")
def fake_event():
    return jsonify(add_event("test", "Test alert", 0.99, latest["jpeg"], 1, "info", None, latest["jpeg"]))


@app.get("/media/<path:name>")
def media(name):
    return send_from_directory(MEDIA, name)


if __name__ == "__main__":
    if not SOURCE:
        raise SystemExit("Set SOURCE to your video file first, e.g.  set SOURCE=C:\\videos\\test.mp4")
    Engine(SOURCE, DB, state, publish, add_event,
           loop=os.environ.get("LOOP") == "1", realtime=os.environ.get("REALTIME") == "1",
           start=os.environ.get("START")).start()
    print("Argus backend on http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, threaded=True)