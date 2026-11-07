"""Re-linking: join a new tracker id to the person who just disappeared (e.g. walked behind a shelf).

Used by engine.py. This is the look() function and Relinker class from track_paths.py, unchanged.
"""
import math
from collections import defaultdict

import cv2
import numpy as np

MAX_GAP_S = 8          # hidden longer than this = treated as a different person
APPEARANCE_MAX = 0.5   # 0 = same clothes colors, 1 = totally different. Raise to link more eagerly
SAMPLES_NEEDED = 5     # frames of a new id used to learn its look before trying to link it
SLACK_FRAC = 0.08      # a person may reappear this far (fraction of frame width) from where they vanished
SPEED_FRAC = 0.25      # ...plus this much per second hidden (the shelf can hide them while they walk on)


def look(frame, box):
    """A color fingerprint of the person's torso, or None if the box is too small.

    Colorful pixels are described by their hue only, so walking into a shadow (darker, same shirt)
    barely changes it. Grey/black/white pixels have no real hue, so they are described by brightness.
    """
    x1, y1, x2, y2 = [int(v) for v in box]
    bw, bh = x2 - x1, y2 - y1
    if bw < 12 or bh < 24:
        return None
    fh, fw = frame.shape[:2]
    tx1, tx2 = max(x1 + int(0.2 * bw), 0), min(x2 - int(0.2 * bw), fw)
    ty1, ty2 = max(y1 + int(0.15 * bh), 0), min(y1 + int(0.6 * bh), fh)
    if tx2 - tx1 < 4 or ty2 - ty1 < 4:
        return None
    hsv = cv2.cvtColor(frame[ty1:ty2, tx1:tx2], cv2.COLOR_BGR2HSV).reshape(-1, 3).astype(np.float32)
    hue, sat, val = hsv[:, 0], hsv[:, 1], hsv[:, 2]
    colorful = (sat >= 60) & (val >= 50)
    share = float(colorful.mean())
    hue_hist = np.histogram(hue[colorful], bins=18, range=(0, 180))[0].astype(np.float32)
    hue_hist = np.convolve(np.r_[hue_hist[-2:], hue_hist, hue_hist[:2]], [1, 2, 3, 2, 1], "valid")  # smooth; hue wraps
    val_hist = np.histogram(val[~colorful], bins=6, range=(0, 256))[0].astype(np.float32)
    val_hist = np.convolve(np.r_[val_hist[0], val_hist, val_hist[-1]], [1, 2, 1], "valid")
    fp = np.r_[hue_hist / max(hue_hist.sum(), 1e-9) * share, val_hist / max(val_hist.sum(), 1e-9) * (1 - share)]
    return (fp / max(fp.sum(), 1e-9)).astype(np.float32)


class Relinker:
    def __init__(self, fps, width):
        self.fps, self.width = fps, width
        self.alias = {}        # tracker id -> the earlier person it was linked to
        self.first = {}        # tracker id -> (frame, x, y) where it first appeared
        self.last = {}         # tracker id -> (frame, x, y) where it was last seen
        self.sig = {}          # tracker id -> [sum of look histograms, count]
        self.pending = set()   # new tracker ids waiting to be linked
        self.log = []

    def stable(self, raw):
        return self.alias.get(raw, raw)

    def group_look(self, person):
        """(average look, number of frames it is based on) for a person, over all their tracker ids."""
        sigs = [s for raw, s in self.sig.items() if self.stable(raw) == person]
        n = sum(s[1] for s in sigs)
        return (sum(s[0] for s in sigs) / n, n) if n else (None, 0)

    def update(self, frame_no, frame, ids, boxes):
        for raw, box in zip(ids, boxes):
            x, y = float((box[0] + box[2]) / 2), float(box[3])
            if raw not in self.first:
                self.first[raw] = (frame_no, x, y)
                self.pending.add(raw)
            self.last[raw] = (frame_no, x, y)
            h = look(frame, box)
            if h is not None:
                s = self.sig.setdefault(raw, [np.zeros_like(h), 0])
                if s[1] < 30:
                    s[0] += h
                    s[1] += 1
        for raw in list(self.pending):
            if raw in self.sig and self.sig[raw][1] >= SAMPLES_NEEDED:
                self.pending.discard(raw)
                self._try_link(raw)
            elif frame_no - self.first[raw][0] > 3 * self.fps:
                self.pending.discard(raw)    # never got a usable look: leave it as its own person

    def _try_link(self, raw):
        f0, x0, y0 = self.first[raw]
        mine = self.sig[raw][0] / self.sig[raw][1]
        groups = defaultdict(list)           # earlier people, each with all the tracker ids they own
        for other in self.last:
            if other != raw:
                groups[self.stable(other)].append(other)
        best = None
        for g, members in groups.items():
            last_frame, lx, ly = max((self.last[m] for m in members), key=lambda t: t[0])
            gap = f0 - last_frame
            if gap < 1 or gap > MAX_GAP_S * self.fps:     # still on screen, or gone too long
                continue
            sigs = [self.sig[m] for m in members if m in self.sig]
            if not sigs:
                continue
            theirs = sum(s[0] for s in sigs) / sum(s[1] for s in sigs)
            app = cv2.compareHist(mine.astype(np.float32), theirs.astype(np.float32), cv2.HISTCMP_BHATTACHARYYA)
            gap_s = gap / self.fps
            allowed = (SLACK_FRAC + SPEED_FRAC * gap_s) * self.width
            dist = math.hypot(x0 - lx, y0 - ly)
            if app > APPEARANCE_MAX or dist > allowed:
                continue
            score = app + 0.5 * dist / allowed
            if best is None or score < best[0]:
                best = (score, g, gap_s, app, dist)
        if best:
            _, g, gap_s, app, dist = best
            self.alias[raw] = g
            entry = {"new_tracker_id": int(raw), "linked_to_person": int(g), "at_frame": int(f0),
                     "hidden_seconds": round(gap_s, 2), "look_difference": round(float(app), 3),
                     "moved_px": round(dist)}
            self.log.append(entry)
            print(f"re-linked id {raw} -> person #{g}  (hidden {gap_s:.1f} s, look diff {app:.2f}, moved {dist:.0f} px)")