"""Argus alert rules: storage, validation and the schedule check.

A rule says WHERE (a named area, or the whole view when area_id is None), WHAT (condition + number),
WHEN (days and time windows, which may cross midnight), and HOW LOUD (severity + cooldown).

Conditions and what `param` means:
  presence    anyone is there                    (no number)
  loiter      a person stays longer than N       (seconds)
  crowd       N or more people at once           (people)
  unattended  nobody there for N                 (minutes)
After-hours is just presence (or any condition) with a schedule that covers the hours you care about.
"""
import json
import sqlite3

CONDITIONS = ("presence", "loiter", "crowd", "unattended")
SEVERITIES = ("info", "warning", "critical")

SCHEMA = """
CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT, cam INTEGER DEFAULT 1,
    area_id INTEGER,                      -- NULL = whole view
    kind TEXT NOT NULL, param REAL, severity TEXT DEFAULT 'warning',
    cooldown INTEGER DEFAULT 60,          -- seconds before the same person / rule can alert again
    schedule TEXT,                        -- JSON {"days": [0-6, Monday=0], "windows": [["22:00", "06:00"]], "outside": false}
    enabled INTEGER DEFAULT 1);
"""


def init_rules(path):
    with sqlite3.connect(path) as c:
        c.executescript(SCHEMA)


def _conn(path):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c


def _row(r):
    d = dict(r)
    d["condition"] = d.pop("kind")
    s = json.loads(d.get("schedule") or "{}")
    d["schedule"] = {"days": s.get("days", []), "windows": s.get("windows", []), "outside": bool(s.get("outside"))}
    d["enabled"] = bool(d["enabled"])
    return d


def _mins(s):
    h, m = str(s).split(":")
    h, m = int(h), int(m)
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError("bad time")
    return h * 60 + m


def clean(d):
    """Validate a rule body and return it in a normal form. Raises ValueError with a readable message."""
    kind = d.get("condition")
    if kind not in CONDITIONS:
        raise ValueError("unknown condition")
    severity = d.get("severity", "warning")
    if severity not in SEVERITIES:
        raise ValueError("unknown severity")
    area = d.get("area_id")
    if area in ("", None):
        area = None
    else:
        try:
            area = int(area)
        except (TypeError, ValueError):
            raise ValueError("bad area")
    param = None
    if kind != "presence":
        try:
            param = float(d.get("param"))
        except (TypeError, ValueError):
            raise ValueError("this rule needs a number")
        low = {"loiter": 1, "crowd": 2, "unattended": 0.1}[kind]
        if not low <= param <= 100000:
            raise ValueError(f"the number must be at least {low}")
        if kind == "crowd":
            param = int(param)
    try:
        cooldown = int(d.get("cooldown", 60))
    except (TypeError, ValueError):
        raise ValueError("bad cooldown")
    if not 0 <= cooldown <= 86400:
        raise ValueError("cooldown must be between 0 and 86400 seconds")
    s = d.get("schedule") or {}
    try:
        days = sorted({int(x) for x in s.get("days", [])})
        windows = [[str(a), str(b)] for a, b in s.get("windows", [])]
        for a, b in windows:
            _mins(a), _mins(b)
    except (TypeError, ValueError):
        raise ValueError("bad schedule")
    if any(not 0 <= x <= 6 for x in days) or len(windows) > 4:
        raise ValueError("bad schedule")
    return {"area_id": area, "condition": kind, "param": param, "severity": severity, "cooldown": cooldown,
            "schedule": {"days": days, "windows": windows, "outside": bool(s.get("outside")) and bool(windows)},
            "enabled": bool(d.get("enabled", True))}


def list_rules(path, cam=1):
    with _conn(path) as c:
        return [_row(r) for r in c.execute("SELECT * FROM rules WHERE cam=? ORDER BY id", (cam,))]


def get_rule(path, rid):
    with _conn(path) as c:
        r = c.execute("SELECT * FROM rules WHERE id=?", (rid,)).fetchone()
    return _row(r) if r else None


def add_rule(path, data, cam=1):
    v = clean(data)
    with _conn(path) as c:
        rid = c.execute("INSERT INTO rules (cam, area_id, kind, param, severity, cooldown, schedule, enabled) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (cam, v["area_id"], v["condition"], v["param"], v["severity"], v["cooldown"],
                         json.dumps(v["schedule"]), int(v["enabled"]))).lastrowid
    return get_rule(path, rid)


def update_rule(path, rid, data):
    cur = get_rule(path, rid)
    if not cur:
        return None
    v = clean({**cur, **data})
    with _conn(path) as c:
        c.execute("UPDATE rules SET area_id=?, kind=?, param=?, severity=?, cooldown=?, schedule=?, enabled=? WHERE id=?",
                  (v["area_id"], v["condition"], v["param"], v["severity"], v["cooldown"],
                   json.dumps(v["schedule"]), int(v["enabled"]), rid))
    return get_rule(path, rid)


def delete_rule(path, rid):
    with _conn(path) as c:
        c.execute("DELETE FROM rules WHERE id=?", (rid,))


def delete_for_area(path, area_id):
    """Rules that point at a deleted area would never fire, so they go with it."""
    with _conn(path) as c:
        c.execute("DELETE FROM rules WHERE area_id=?", (area_id,))


def schedule_active(schedule, dt):
    """Is the schedule on at this moment (a datetime)? Windows may cross midnight: 22:00-06:00 started yesterday
    counts for the early hours, so the day list refers to the day the window starts."""
    days = schedule.get("days") or []
    windows = schedule.get("windows") or []
    today = dt.weekday()
    if not windows:
        return not days or today in days
    cur = dt.hour * 60 + dt.minute
    for start, end in windows:
        a, b = _mins(start), _mins(end)
        if a == b:                                      # same start and end: the whole day
            if not days or today in days:
                return True
        elif a < b:                                     # same-day window
            if a <= cur < b and (not days or today in days):
                return True
        else:                                           # crosses midnight
            if cur >= a and (not days or today in days):
                return True
            if cur < b and (not days or (today - 1) % 7 in days):
                return True
    return False
