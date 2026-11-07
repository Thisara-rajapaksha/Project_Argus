"""Argus step 1: detect and track people in a video with YOLO, store everything in SQLite.

Install (once):
    pip install ultralytics
    pip install torch torchvision --index-url https://download.pytorch.org/whl/cu124   # CUDA build for the RTX 4050

Run:
    python track_people.py cctv.mp4
    python track_people.py cctv.mp4 --start "2026-10-26 22:00:00" --save-video out.mp4
    python track_people.py cctv.mp4 --model yolo11s.pt --imgsz 960      # lighter, if the GPU runs hot

What it does
  * YOLO finds people (class 0 only), BoT-SORT follows them and gives each a track id.
  * Positions go into SQLite (argus.db), sampled at --sample-fps per second.
  * `start` is the video's virtual clock: frame N happens at start + N / video_fps.
  * At the end it prints a report that flags possible ID splits (one person who got two ids),
    because "one person = one id" is the main requirement.
"""
import argparse
import math
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import cv2
import torch
from ultralytics import YOLO

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video TEXT, started_at TEXT, processed_at TEXT,
    fps REAL, width INTEGER, height INTEGER, frames INTEGER,
    model TEXT, imgsz INTEGER, conf REAL, params TEXT
);
CREATE TABLE IF NOT EXISTS tracks (            -- one row per person (track id) per run
    run_id INTEGER, track_id INTEGER,
    first_t REAL, last_t REAL, first_ts TEXT, last_ts TEXT,
    frames INTEGER, avg_conf REAL,
    start_x REAL, start_y REAL, end_x REAL, end_y REAL,
    PRIMARY KEY (run_id, track_id)
);
CREATE TABLE IF NOT EXISTS positions (         -- sampled positions, all coordinates in 0..1
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER, track_id INTEGER,
    frame INTEGER, t REAL, ts TEXT,            -- t = seconds into the video, ts = virtual wall-clock
    x REAL, y REAL,                            -- feet point (bottom-centre of the box)
    x1 REAL, y1 REAL, x2 REAL, y2 REAL,        -- the box
    conf REAL
);
CREATE INDEX IF NOT EXISTS idx_pos_run_track ON positions (run_id, track_id, frame);
CREATE INDEX IF NOT EXISTS idx_pos_run_t ON positions (run_id, t);
"""

TRACKER_YAML = """tracker_type: botsort
track_high_thresh: 0.25
track_low_thresh: 0.1
new_track_thresh: 0.4
track_buffer: {buffer}        # frames a lost person is remembered (longer = survives occlusion)
match_thresh: 0.8
fuse_score: true
gmc_method: none              # the camera is stationary, so skip camera-motion compensation
proximity_thresh: 0.5
appearance_thresh: 0.8
with_reid: {reid}
model: auto
"""


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("video", help="path to the video file")
    p.add_argument("--db", default="argus.db")
    p.add_argument("--model", default="yolo11m.pt", help="yolo11m fits a 6 GB card; use yolo11s if it is too slow")
    p.add_argument("--imgsz", type=int, default=960, help="inference size; raise to 1280 for small, far-away people")
    p.add_argument("--conf", type=float, default=0.35)
    p.add_argument("--sample-fps", type=float, default=5, help="positions saved per second")
    p.add_argument("--start", default=None, help="virtual start time 'YYYY-MM-DD HH:MM:SS' (default: now)")
    p.add_argument("--buffer-seconds", type=float, default=3, help="how long a lost person is remembered")
    p.add_argument("--no-reid", action="store_true", help="turn off appearance matching (needs a recent ultralytics)")
    p.add_argument("--save-video", default=None, help="write an annotated video here")
    p.add_argument("--show", action="store_true", help="show a live preview window (q to quit)")
    p.add_argument("--split-gap", type=float, default=8, help="seconds: max gap between two tracks to flag a split")
    p.add_argument("--split-dist", type=float, default=0.15, help="max distance (0..1) between where one ended and the next began")
    return p.parse_args()


def split_report(tracks, fps, gap_s, max_dist, min_frames):
    """Print tracks that look like the same person split in two, and very short tracks."""
    items = sorted(tracks.items(), key=lambda kv: kv[1]["first"])
    short = [tid for tid, t in items if t["n"] < min_frames]
    print(f"\nPeople (track ids): {len(items)}")
    if short:
        print(f"  Very short tracks (< {min_frames} frames, probably noise): {short}")
    pairs = []
    for ida, a in items:
        for idb, b in items:
            if idb == ida or b["first"] <= a["last"]:
                continue
            gap = (b["first"] - a["last"]) / fps
            if gap > gap_s:
                continue
            d = math.hypot(a["end"][0] - b["start"][0], a["end"][1] - b["start"][1])
            if d <= max_dist:
                pairs.append((ida, idb, gap, d))
    if pairs:
        print("  Possible ID splits (one person, two ids):")
        for ida, idb, gap, d in pairs:
            print(f"    id {ida} ended, id {idb} began {gap:.1f}s later, {d:.2f} away")
        print("  Check these in the annotated video, then raise --buffer-seconds or tune the Relinker.")
    else:
        print("  No suspected ID splits.")


def main():
    a = parse_args()
    video = Path(a.video)
    if not video.is_file():
        sys.exit(f"Video not found: {video}")

    cap = cv2.VideoCapture(str(video))
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    width, height = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()

    cuda = torch.cuda.is_available()
    print("GPU:", torch.cuda.get_device_name(0) if cuda else "none found, running on CPU (slow)")
    start = datetime.strptime(a.start, "%Y-%m-%d %H:%M:%S") if a.start else datetime.now().replace(microsecond=0)

    tracker_file = Path("argus_botsort.yaml")
    tracker_file.write_text(TRACKER_YAML.format(buffer=int(fps * a.buffer_seconds), reid=str(not a.no_reid).lower()))

    db = sqlite3.connect(a.db)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    run_id = db.execute(
        "INSERT INTO runs (video, started_at, processed_at, fps, width, height, frames, model, imgsz, conf, params) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (str(video), start.isoformat(sep=" "), datetime.now().isoformat(sep=" ", timespec="seconds"),
         fps, width, height, total, a.model, a.imgsz, a.conf, repr(vars(a)))).lastrowid
    db.commit()
    print(f"Run {run_id}: {video.name}  {width}x{height} @ {fps:.1f} fps, {total} frames, virtual start {start}")

    model = YOLO(a.model)
    stride = max(1, round(fps / a.sample_fps))       # save a position every `stride` frames
    results = model.track(source=str(video), stream=True, persist=True, classes=[0], conf=a.conf,
                          imgsz=a.imgsz, half=cuda, device=0 if cuda else "cpu",
                          tracker=str(tracker_file), verbose=False)

    writer = None
    tracks, buf = {}, []
    frame_no, t0 = -1, time.time()

    def flush():
        if buf:
            db.executemany("INSERT INTO positions (run_id, track_id, frame, t, ts, x, y, x1, y1, x2, y2, conf) "
                           "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", buf)
            db.commit()
            buf.clear()

    try:
        for r in results:
            frame_no += 1
            boxes = r.boxes
            if boxes is not None and boxes.id is not None:
                ids = boxes.id.int().tolist()
                xyxyn = boxes.xyxyn.tolist()
                confs = boxes.conf.tolist()
                t = frame_no / fps
                ts = (start + timedelta(seconds=t)).isoformat(sep=" ", timespec="milliseconds")
                for tid, (x1, y1, x2, y2), c in zip(ids, xyxyn, confs):
                    fx, fy = (x1 + x2) / 2, y2
                    tr = tracks.setdefault(tid, {"first": frame_no, "start": (fx, fy), "n": 0, "conf": 0.0})
                    tr["last"], tr["end"] = frame_no, (fx, fy)
                    tr["n"] += 1
                    tr["conf"] += c
                    if frame_no % stride == 0:
                        buf.append((run_id, tid, frame_no, round(t, 3), ts, fx, fy, x1, y1, x2, y2, c))
                if len(buf) >= 200:
                    flush()

            if a.save_video or a.show:
                img = r.plot()                      # frame with boxes and ids drawn
                if a.save_video and writer is None:
                    writer = cv2.VideoWriter(a.save_video, cv2.VideoWriter_fourcc(*"mp4v"), fps, (img.shape[1], img.shape[0]))
                if writer:
                    writer.write(img)
                if a.show:
                    cv2.imshow("Argus", img)
                    if cv2.waitKey(1) & 0xFF == ord("q"):
                        break

            if frame_no % 100 == 0:
                print(f"\r  frame {frame_no}/{total}  {frame_no / (time.time() - t0):.1f} fps  people so far: {len(tracks)}", end="")
    except KeyboardInterrupt:
        print("\nStopped early, saving what was processed.")
    finally:
        flush()
        for tid, t in tracks.items():
            db.execute("INSERT OR REPLACE INTO tracks VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                       (run_id, tid, t["first"] / fps, t["last"] / fps,
                        (start + timedelta(seconds=t["first"] / fps)).isoformat(sep=" ", timespec="seconds"),
                        (start + timedelta(seconds=t["last"] / fps)).isoformat(sep=" ", timespec="seconds"),
                        t["n"], t["conf"] / t["n"], *t["start"], *t["end"]))
        db.execute("UPDATE runs SET frames=? WHERE id=?", (frame_no + 1, run_id))
        db.commit()
        db.close()
        if writer:
            writer.release()
        cv2.destroyAllWindows()

    print(f"\nDone: {frame_no + 1} frames in {time.time() - t0:.0f}s. Saved to {a.db} (run {run_id}).")
    split_report(tracks, fps, a.split_gap, a.split_dist, min_frames=int(fps * 0.5))


if __name__ == "__main__":
    main()