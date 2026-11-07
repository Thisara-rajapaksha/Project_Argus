"""Argus engine: reads a video, tracks every person (YOLO + BoT-SORT + your Relinker), saves their
positions and photos to SQLite while the video plays, and runs the alert rules (rules.py) on every frame.

Needs relink.py, areas.py and rules.py next to it.
"""
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import cv2
import numpy as np
import torch
import ultralytics
import yaml
from ultralytics import YOLO

import areas
import rules
from relink import Relinker

HERE = os.path.dirname(os.path.abspath(__file__))
MIN_POINTS = 15        # people seen in fewer frames than this are flicker and are not saved as people
TRACK_BUFFER = 150     # frames the tracker remembers a lost person
PROXIMITY_THRESH = 0.2
IMGSZ = int(os.environ.get("IMGSZ", "1920"))
SAVE_EVERY = 1.0       # seconds between database writes (positions, people rows, new photos)
RELOAD_EVERY = 2.0     # seconds between re-reading areas and rules, so edits apply while the video plays

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, video TEXT, started_at TEXT, processed_at TEXT,
    fps REAL, width INTEGER, height INTEGER, frames INTEGER, model TEXT);
CREATE TABLE IF NOT EXISTS positions (        -- sampled; x, y = feet point, box = x1..y2, all in 0..1
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, person INTEGER, frame INTEGER,
    t REAL, ts TEXT, x REAL, y REAL, x1 REAL, y1 REAL, x2 REAL, y2 REAL, conf REAL,
    zone INTEGER);                            -- zone = id of the highest-priority labelled area the person was in, or NULL
CREATE INDEX IF NOT EXISTS idx_pos ON positions (run_id, person, frame);
CREATE TABLE IF NOT EXISTS persons (          -- one row per real person in a run
    run_id INTEGER, person INTEGER, first_t REAL, last_t REAL, first_ts TEXT, last_ts TEXT,
    frames INTEGER, tracker_ids TEXT, start_x REAL, start_y REAL, end_x REAL, end_y REAL,
    snapshot TEXT,                            -- best photo of the person, e.g. /media/people/run3_person7.jpg
    PRIMARY KEY (run_id, person));
"""


def init_db(path):
    with sqlite3.connect(path) as c:
        c.execute("PRAGMA journal_mode=WAL")      # lets the web server read while the engine writes every second
        c.executescript(SCHEMA)
        if "snapshot" not in [r[1] for r in c.execute("PRAGMA table_info(persons)")]:
            c.execute("ALTER TABLE persons ADD COLUMN snapshot TEXT")   # database made by an older version


def reid_config():
    """BoT-SORT config from your ultralytics, with appearance matching switched on."""
    base = Path(ultralytics.__file__).parent / "cfg" / "trackers" / "botsort.yaml"
    cfg = yaml.safe_load(base.read_text())
    cfg.update(with_reid=True, model="auto", track_buffer=TRACK_BUFFER, proximity_thresh=PROXIMITY_THRESH)
    out = Path(HERE) / "botsort_reid.yaml"
    out.write_text(yaml.safe_dump(cfg))
    return str(out)


class Engine:
    def __init__(self, source, db_path, state, publish, add_event, loop=False, realtime=False, start=None, sample_fps=5):
        self.source, self.db_path, self.state, self.publish = source, db_path, state, publish
        self.add_event = add_event       # add_event(kind, label, score, jpeg, cam, severity=, rule_id=, frame_jpeg=)
        self.loop, self.realtime, self.sample_fps = loop, realtime, sample_fps
        self.start_time = datetime.strptime(start, "%Y-%m-%d %H:%M:%S") if start else datetime.now().replace(microsecond=0)
        self._areas, self._rules, self._loaded, self._hot_until = [], [], 0.0, 0.0

    def start(self):
        init_db(self.db_path)
        areas.init_areas(self.db_path)
        rules.init_rules(self.db_path)
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        cuda = torch.cuda.is_available()
        print("GPU:", torch.cuda.get_device_name(0) if cuda else "none, running on CPU (slow)")
        model = YOLO(os.environ.get("MODEL", "yolo11m.pt"))
        cfg = reid_config()
        while True:
            try:
                ran = self._pass(model, cfg, cuda)
            except Exception as e:                       # keep the thread alive
                print("engine error:", e)
                ran = False
            if ran and not self.loop:
                self.state["finished"] = True
                return
            time.sleep(2)

    # ---------- areas and rules, re-read every couple of seconds ----------
    def _reload(self, w, h):
        try:
            ars = areas.list_areas(self.db_path)
            self._rules = rules.list_rules(self.db_path, 1)
        except Exception as e:
            print("could not read areas/rules:", e)
            self._loaded = time.time()
            return
        self._areas = []
        for a in ars:
            poly = a.get("polygon") or []
            if len(poly) < 3:
                continue
            px = np.array([[int(x * w), int(y * h)] for x, y in poly], np.int32).reshape(-1, 1, 2)
            self._areas.append({"id": a["id"], "name": a["name"], "priority": a.get("priority") or 0,
                                "center": a.get("anchor") == "center", "px": px})
        self._loaded = time.time()

    def _evaluate(self, rs, t, now_dt, dets, frame, best):
        """Run every enabled rule on this frame. A person counts as inside every area they stand in.
        dets = [(person, {area ids}, conf, (x1, y1, x2, y2)), ...]. Returns True if any alert fired."""
        names = {a["id"]: a["name"] for a in self._areas}
        everyone = {d[0] for d in dets}
        seen = {d[0]: (d[2], d[3]) for d in dets}
        fired = False

        def ready(key, cooldown):                        # cooldown runs on the video clock
            last = rs["fired"].get(key)
            if last is not None and t - last < cooldown:
                return False
            rs["fired"][key] = t
            return True

        def fire(rule, where, text, person=None):
            nonlocal fired
            crop, score = None, 0.0
            if person is not None:
                score, (x1, y1, x2, y2) = seen[person]
                src = best[person][1] if person in best else frame[max(int(y1), 0):int(y2), max(int(x1), 0):int(x2)]
                if src.size:
                    ok_c, cj = cv2.imencode(".jpg", src)
                    crop = cj.tobytes() if ok_c else None
            ok_f, fj = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
            self.add_event(rule["condition"], f"{where}: {text}", score, crop, 1, severity=rule["severity"],
                           rule_id=rule["id"], frame_jpeg=fj.tobytes() if ok_f else None)
            fired = True

        for r in self._rules:
            rid = r["id"]
            if r["area_id"] is not None and r["area_id"] not in names:
                continue                                  # its area was deleted
            if not (r["enabled"] and self.state["armed"] and rules.schedule_active(r["schedule"], now_dt)):
                rs["empty"].pop(rid, None)                # off right now: timers start again when it is back on
                for k in [k for k in rs["dwell"] if k[0] == rid]:
                    del rs["dwell"][k]
                continue
            where = "Whole view" if r["area_id"] is None else names[r["area_id"]]
            scope = everyone if r["area_id"] is None else {p for p, ids, _, _ in dets if r["area_id"] in ids}
            kind, n, cool = r["condition"], r["param"], r["cooldown"]
            if kind == "presence":
                for p in scope:
                    if ready((rid, p), cool):
                        fire(r, where, f"person #{p} detected", p)
            elif kind == "loiter":
                for p in scope:
                    d = rs["dwell"].setdefault((rid, p), [t, t])
                    d[1] = t
                    if t - d[0] >= n and ready((rid, p), cool):
                        fire(r, where, f"person #{p} stayed {t - d[0]:.0f} s", p)
            elif kind == "crowd":
                if len(scope) >= n and ready((rid, "*"), cool):
                    fire(r, where, f"{len(scope)} people (limit {int(n)})")
            elif kind == "unattended":
                if scope:
                    rs["empty"].pop(rid, None)
                else:
                    since = rs["empty"].setdefault(rid, t)
                    if t - since >= n * 60 and ready((rid, "*"), cool):
                        fire(r, where, f"nobody for {(t - since) / 60:.1f} min")
        for k in [k for k, d in rs["dwell"].items() if t - d[1] > 1.5]:
            del rs["dwell"][k]                            # left the area: the timer starts again next time
        return fired

    def _pass(self, model, cfg, cuda):
        cap = cv2.VideoCapture(self.source)
        if not cap.isOpened():
            self.state["online"] = False
            print("Can't open the video:", self.source)
            return False
        fps = cap.get(cv2.CAP_PROP_FPS) or 25
        w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        model.predictor = None                           # forget the old tracker: every pass starts clean
        rl = Relinker(fps, w)
        db = sqlite3.connect(self.db_path)
        run_id = db.execute("INSERT INTO runs (video, started_at, processed_at, fps, width, height, model) "
                            "VALUES (?,?,?,?,?,?,?)",
                            (str(self.source), self.start_time.isoformat(sep=" "),
                             datetime.now().isoformat(sep=" ", timespec="seconds"), fps, w, h,
                             os.environ.get("MODEL", "yolo11m.pt"))).lastrowid
        db.commit()
        self.state.update(online=True, finished=False, alerts=[], alert=False, t=0.0)

        stride = max(1, round(fps / self.sample_fps))
        tracks, raws, best, buf = {}, {}, {}, []
        dirty, photos = set(), {}                        # people whose photo improved / photo paths already saved
        rs = {"dwell": {}, "empty": {}, "fired": {}}     # rule timers for this pass
        frame_no, n_frames, t_fps, last_save = 0, 0, time.time(), time.time()
        self._loaded = 0.0
        while True:
            t_loop = time.time()
            ok, frame = cap.read()
            if not ok:
                break
            if time.time() - self._loaded >= RELOAD_EVERY:
                self._reload(w, h)
            r = model.track(frame, persist=True, conf=0.2, imgsz=IMGSZ, classes=[0], iou=0.5,
                            device=0 if cuda else "cpu", half=cuda, verbose=False, tracker=cfg)[0]
            t = frame_no / fps
            now_dt = self.start_time + timedelta(seconds=t)
            ts = now_dt.isoformat(sep=" ", timespec="milliseconds")
            dets = []
            if r.boxes.id is not None:
                ids = r.boxes.id.int().cpu().tolist()
                boxes = r.boxes.xyxy.cpu().numpy()
                confs = r.boxes.conf.cpu().tolist()
                rl.update(frame_no, frame, ids, boxes)
                for raw, box, c in zip(ids, boxes, confs):
                    person = rl.stable(raw)
                    x1, y1, x2, y2 = [float(v) for v in box]
                    fx, fy = (x1 + x2) / 2, y2
                    raws.setdefault(person, set()).add(int(raw))
                    tr = tracks.setdefault(person, {"first": frame_no, "start": (fx / w, fy / h), "n": 0})
                    tr["last"], tr["end"], tr["n"] = frame_no, (fx / w, fy / h), tr["n"] + 1

                    inside = []                          # every labelled area this person stands in
                    for a in self._areas:
                        qx, qy = ((x1 + x2) / 2, (y1 + y2) / 2) if a["center"] else (fx, fy)
                        if cv2.pointPolygonTest(a["px"], (float(qx), float(min(qy, h - 1))), False) >= 0:
                            inside.append(a)
                    top = max(inside, key=lambda a: a["priority"])["id"] if inside else None
                    dets.append((person, {a["id"] for a in inside}, float(c), (x1, y1, x2, y2)))

                    if frame_no % stride == 0:
                        buf.append((run_id, person, frame_no, round(t, 3), ts, fx / w, fy / h,
                                    x1 / w, y1 / h, x2 / w, y2 / h, c, top))
                    area = (x2 - x1) * (y2 - y1)         # remember the biggest uncut photo of each person
                    if x1 > 2 and y1 > 2 and x2 < w - 2 and y2 < h - 2 and area > best.get(person, (0, None))[0] * 1.1:
                        best[person] = (area, frame[int(y1):int(y2), int(x1):int(x2)].copy())
                        dirty.add(person)
                    cv2.rectangle(frame, (int(x1), int(y1)), (int(x2), int(y2)), (255, 200, 0), 2)
                    cv2.putText(frame, f"#{person}", (int(x1), max(int(y1) - 6, 14)),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 200, 0), 2)

            if self._evaluate(rs, t, now_dt, dets, frame, best):
                self._hot_until = time.time() + 5        # keeps the camera pulsing in the dashboard for a few seconds
            hot = time.time() < self._hot_until
            self.state["alerts"] = [1] if hot else []
            self.state["alert"] = hot
            self.state["people"] = len(dets)
            self.state["t"] = t                          # current video second, used to tell who is still in view
            ok_p, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
            if ok_p:
                self.publish(jpg.tobytes())

            if time.time() - last_save >= SAVE_EVERY or len(buf) >= 200:
                self._checkpoint(db, run_id, fps, tracks, raws, best, dirty, photos, buf)
                last_save = time.time()
            frame_no += 1
            n_frames += 1
            if time.time() - t_fps >= 1:
                self.state["fps"], n_frames, t_fps = round(n_frames / (time.time() - t_fps), 1), 0, time.time()
            if self.realtime:
                time.sleep(max(0.0, 1 / fps - (time.time() - t_loop)))

        cap.release()
        self._checkpoint(db, run_id, fps, tracks, raws, best, dirty, photos, buf)
        usable = [p for p, t in tracks.items() if t["n"] >= MIN_POINTS]
        db.execute("UPDATE runs SET frames=? WHERE id=?", (frame_no, run_id))
        db.commit()
        db.close()
        try:                                             # who was in which labelled area
            print(f"{areas.rebuild(self.db_path, run_id)} area visits saved")
        except Exception as e:
            print("area history error:", e)
        n_raw = len({r for s in raws.values() for r in s})
        print(f"Run {run_id}: {n_raw} tracker ids -> {len(usable)} people "
              f"({len(rl.log)} re-linked, {len(tracks) - len(usable)} dropped as too short)")
        return True

    def _checkpoint(self, db, run_id, fps, tracks, raws, best, dirty, photos, buf):
        """Write everything the dashboard needs to show people live: positions, new photos, and one row per person."""
        self._flush(db, buf)
        folder = os.path.join(HERE, "media", "people")
        os.makedirs(folder, exist_ok=True)
        for p in list(dirty):                            # photo got better: save it again
            t = tracks.get(p)
            if p in best and t and t["n"] >= MIN_POINTS:
                name = f"run{run_id}_person{p}.jpg"
                crop = best[p][1]
                scale = min(1.0, 320 / crop.shape[0])
                cv2.imwrite(os.path.join(folder, name),
                            cv2.resize(crop, None, fx=scale, fy=scale) if scale < 1 else crop)
                photos[p] = f"/media/people/{name}"
                dirty.discard(p)
        for p, t in tracks.items():
            if t["n"] < MIN_POINTS:                      # flicker, not a person yet
                continue
            db.execute("INSERT OR REPLACE INTO persons (run_id, person, first_t, last_t, first_ts, last_ts, frames, "
                       "tracker_ids, start_x, start_y, end_x, end_y, snapshot) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (run_id, p, t["first"] / fps, t["last"] / fps,
                        (self.start_time + timedelta(seconds=t["first"] / fps)).isoformat(sep=" ", timespec="seconds"),
                        (self.start_time + timedelta(seconds=t["last"] / fps)).isoformat(sep=" ", timespec="seconds"),
                        t["n"], ",".join(map(str, sorted(raws[p]))), *t["start"], *t["end"], photos.get(p)))
        db.commit()

    @staticmethod
    def _flush(db, buf):
        if buf:
            db.executemany("INSERT INTO positions (run_id, person, frame, t, ts, x, y, x1, y1, x2, y2, conf, zone) "
                           "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", buf)
            db.commit()
            buf.clear()