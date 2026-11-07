"""Argus MCP server: lets an LLM read what the camera saw, compute summaries, and PROPOSE rule changes.

Design rules:
  * Read and compute tools query guard.db through a read-only SQLite connection. Every number is produced by SQL,
    and none of them needs the backend to be running.
  * The LLM can never change anything. "Propose" tools only ask the Flask backend to store a pending proposal;
    you approve or reject it in the dashboard (/approvals). This server has no tool that approves, and the backend
    refuses approve/reject requests that carry this server's token header.
  * Every tool call is logged (backend audit table, or mcp_audit.log next to this file if the backend is down).

Install:  pip install "mcp>=1.2"        (works with mcp 1.x and 2.x)
Run:      python argus_mcp.py            (stdio: add it to Claude Desktop, see the bottom of this file)
Env:      ARGUS_DB (default guard.db next to this file), ARGUS_API (default http://127.0.0.1:5000),
          ARGUS_MCP_TOKEN (optional shared secret, must match the backend's)
"""
import functools
import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from typing import Optional

try:                                                  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server
except ImportError:
    try:                                              # mcp 2.x renamed FastMCP to MCPServer
        from mcp.server.mcpserver import MCPServer as _Server
    except ImportError:                               # the standalone fastmcp package
        from fastmcp import FastMCP as _Server

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("ARGUS_DB", os.path.join(HERE, "guard.db"))
API = os.environ.get("ARGUS_API", "http://127.0.0.1:5000").rstrip("/")
TOKEN = os.environ.get("ARGUS_MCP_TOKEN", "")
MIN_SAMPLES = 5                                       # fewer samples than this (about 1 s) is flicker, not a person
FMT = "%Y-%m-%d %H:%M:%S"
# a real person: enough samples. (run_id, person) is the identity: person ids restart in every run.
VALID = f"(run_id, person) IN (SELECT run_id, person FROM positions GROUP BY run_id, person HAVING COUNT(*) >= {MIN_SAMPLES})"
# when an alert happened, on the video clock (older rows fall back to the wall clock)
EV_TIME = "COALESCE(video_ts, datetime(ts, 'unixepoch', 'localtime'))"

mcp = _Server(
    "argus",
    instructions=(
        "Argus is a security camera system. Use the read and compute tools for facts; never estimate numbers yourself. "
        "Dates are on the video clock, written 2026-10-27 or 2026-10-27 22:00. People are identified by (run_id, person) "
        "and only within one video run: the same video may have been processed several times, so when results mention "
        "overlapping runs, pick one with run_id (see list_runs). Call get_overview first. To change alert rules, use the "
        "propose tools: nothing changes until the user approves the proposal in the Argus dashboard."))


class ToolError(Exception):
    pass


# ---------------------------------------------------------------- plumbing
def q(sql, args=()):
    """Read-only query. The connection cannot write, whatever the SQL says."""
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
    except sqlite3.OperationalError as e:
        raise ToolError(f"Cannot open the database {DB}: {e}")
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA query_only = ON")
        return [dict(r) for r in con.execute(sql, args)]
    except sqlite3.OperationalError as e:
        raise ToolError(f"Database problem: {e}. Has the Argus backend run at least once, and has migrate.py been run?")
    finally:
        con.close()


def one(sql, args=()):
    rows = q(sql, args)
    return rows[0] if rows else {}


def http(method, path, body=None, timeout=5):
    req = urllib.request.Request(
        API + path, method=method, data=None if body is None else json.dumps(body, default=str).encode(),
        headers={"Content-Type": "application/json", "X-Argus-Token": TOKEN})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error") or str(e)
        except Exception:
            msg = str(e)
        raise ToolError(msg)
    except (urllib.error.URLError, OSError):
        raise ToolError("The Argus backend is not running. Start it with: python app.py")


def _log(entry):
    try:
        http("POST", "/api/mcp/audit", entry, timeout=2)
    except Exception:
        try:
            with open(os.path.join(HERE, "mcp_audit.log"), "a", encoding="utf-8") as f:
                f.write(json.dumps({**entry, "ts": time.time()}, default=str) + "\n")
        except OSError:
            pass


def audited(fn):
    """Log every call: tool, arguments, success, how long it took."""
    @functools.wraps(fn)
    def run(*a, **kw):
        t0, ok, note = time.time(), True, ""
        try:
            return fn(*a, **kw)
        except Exception as e:
            ok, note = False, str(e)[:300]
            raise
        finally:
            _log({"tool": fn.__name__, "args": kw or list(a), "ok": ok, "note": note, "ms": int((time.time() - t0) * 1000)})
    return run


def tool(fn):
    return mcp.tool()(audited(fn))


def _parse(s, end=False):
    s = (s or "").strip()
    for fmt, whole in ((FMT, False), ("%Y-%m-%d %H:%M", False), ("%Y-%m-%d", True)):
        try:
            d = datetime.strptime(s, fmt)
        except ValueError:
            continue
        return (d + timedelta(days=1) if end and whole else d).strftime(FMT)
    raise ToolError("Dates look like 2026-10-27 or 2026-10-27 22:00.")


def span(start, end=None):
    """[start, end) as strings. A date alone means that whole day; an end that is a date alone includes that whole day."""
    a = _parse(start)
    if end is None:
        if len((start or "").strip()) != 10:
            raise ToolError("Give an end time as well.")
        b = (datetime.strptime(a, FMT) + timedelta(days=1)).strftime(FMT)
    else:
        b = _parse(end, end=True)
    if b <= a:
        raise ToolError("The end must be after the start.")
    return a, b


def rf(run_id):
    """SQL fragment + args that restrict a positions query to one run."""
    return (" AND run_id = ?", (int(run_id),)) if run_id is not None else ("", ())


def _ev_where(a, b, run_id=None):
    """Alerts in [a, b); with run_id, only those that happened during that run."""
    if run_id is not None:
        r = one("SELECT MIN(ts) AS lo, MAX(ts) AS hi FROM positions WHERE run_id = ?", (int(run_id),))
        if r.get("lo"):
            hi = (datetime.strptime(r["hi"][:19], FMT) + timedelta(seconds=1)).strftime(FMT)
            a, b = max(a, r["lo"][:19]), min(b, hi)
    return f"{EV_TIME} >= ? AND {EV_TIME} < ?", (a, b)


def _runs_note(a, b, run_id):
    if run_id is not None:
        return None
    n = one("SELECT COUNT(DISTINCT run_id) AS n FROM positions WHERE ts >= ? AND ts < ?", (a, b))["n"]
    if n > 1:
        return (f"{n} video runs overlap this period, and totals add them together. People from different runs may be "
                "the same real person (for example a replayed clip). Pass run_id to look at one run; see list_runs.")
    return None


def _areas():
    """{id: area} read straight from the database."""
    return {r["id"]: r for r in q("SELECT id, name, type, note, priority, anchor FROM areas ORDER BY id")}


def _aname(areas, zid):
    if zid is None:
        return None
    return areas[zid]["name"] if zid in areas else f"(deleted area {zid})"


def _dur(s):
    s = int(round(s))
    return f"{s // 60}m {s % 60}s" if s >= 60 else f"{s}s"


def _route(run_id, person, a, b, areas):
    """Labelled areas a person used, in order. Unlabelled stretches are skipped, repeats merged."""
    rows = q("""SELECT zone, ts, MIN(COALESCE(nt - t, 0), 1.0) AS dt FROM (
                  SELECT zone, ts, t, LEAD(t) OVER (ORDER BY frame) AS nt FROM positions
                  WHERE run_id = ? AND person = ? AND ts >= ? AND ts < ?) ORDER BY ts""", (run_id, person, a, b))
    route = []
    for r in rows:
        if r["zone"] is None:
            continue
        if route and route[-1]["zone"] == r["zone"]:
            route[-1]["leave"], route[-1]["seconds"] = r["ts"], route[-1]["seconds"] + r["dt"]
        else:
            route.append({"zone": r["zone"], "enter": r["ts"], "leave": r["ts"], "seconds": r["dt"]})
    return [{"area": _aname(areas, x["zone"]), "enter": x["enter"][:19], "leave": x["leave"][:19],
             "seconds": round(x["seconds"])} for x in route]


def _dwell(a, b, areas, run_id=None):
    ra, rargs = rf(run_id)
    rows = q(f"""SELECT zone, SUM(dt) AS secs, COUNT(DISTINCT run_id || '-' || person) AS people FROM (
                   SELECT run_id, person, zone, MIN(COALESCE(nt - t, 0), 1.0) AS dt FROM (
                     SELECT run_id, person, zone, t, LEAD(t) OVER (PARTITION BY run_id, person ORDER BY frame) AS nt
                     FROM positions WHERE ts >= ? AND ts < ? AND {VALID}{ra}))
                 WHERE zone IS NOT NULL GROUP BY zone ORDER BY secs DESC""", (a, b, *rargs))
    return [{"area": _aname(areas, r["zone"]), "seconds": round(r["secs"]), "people": r["people"]} for r in rows]


def _metrics(a, b, run_id=None):
    """The numbers used by the briefing, the comparison and the counts. All from SQL."""
    ra, rargs = rf(run_id)
    base = f"FROM positions WHERE ts >= ? AND ts < ? AND {VALID}{ra}"
    args = (a, b, *rargs)
    people = one(f"SELECT COUNT(*) AS n FROM (SELECT 1 {base} GROUP BY run_id, person)", args)["n"]
    peak = one(f"SELECT COALESCE(MAX(c), 0) AS n FROM (SELECT COUNT(DISTINCT person) AS c {base} GROUP BY run_id, frame)", args)["n"]
    stay = one(f"SELECT AVG(s) AS n FROM (SELECT MAX(t) - MIN(t) AS s {base} GROUP BY run_id, person)", args)["n"]
    where, eargs = _ev_where(a, b, run_id)
    al = one(f"SELECT COUNT(*) AS n, COALESCE(SUM(severity = 'critical'), 0) AS crit, "
             f"COALESCE(SUM(status = 'false_alarm'), 0) AS fa FROM events WHERE {where}", eargs)
    return {"people": people, "most_at_once": peak, "average_seconds_on_screen": round(stay or 0),
            "alerts": al["n"], "critical_alerts": al["crit"], "false_alarms": al["fa"]}


def _hourly(a, b, run_id=None):
    ra, rargs = rf(run_id)
    people = {r["hr"]: r["n"] for r in q(
        f"SELECT substr(ts, 12, 2) AS hr, COUNT(DISTINCT run_id || '-' || person) AS n FROM positions "
        f"WHERE ts >= ? AND ts < ? AND {VALID}{ra} GROUP BY hr", (a, b, *rargs))}
    peak = {r["hr"]: r["n"] for r in q(
        f"SELECT hr, MAX(c) AS n FROM (SELECT substr(ts, 12, 2) AS hr, COUNT(DISTINCT person) AS c FROM positions "
        f"WHERE ts >= ? AND ts < ? AND {VALID}{ra} GROUP BY hr, run_id, frame) GROUP BY hr", (a, b, *rargs))}
    return people, peak


def _outside(a, b, open_time, close_time, run_id=None):
    """People with any sample outside opening hours (HH:MM). Handles overnight opening hours."""
    for t in (open_time, close_time):
        try:
            datetime.strptime(t, "%H:%M")
        except (TypeError, ValueError):
            raise ToolError("open_time and close_time look like 08:00")
    cond = "(substr(ts, 12, 5) < ? OR substr(ts, 12, 5) >= ?)" if open_time < close_time \
        else "(substr(ts, 12, 5) < ? AND substr(ts, 12, 5) >= ?)"
    ra, rargs = rf(run_id)
    return q(f"""SELECT run_id, person, MIN(ts) AS first_ts, MAX(ts) AS last_ts, COUNT(*) AS samples FROM positions
                 WHERE ts >= ? AND ts < ? AND {cond} AND {VALID}{ra} GROUP BY run_id, person ORDER BY first_ts""",
             (a, b, open_time, close_time, *rargs))


def _with_note(out, note):
    if note:
        out["note"] = note
    return out


# ---------------------------------------------------------------- read tools
@tool
def get_overview() -> dict:
    """What data Argus holds: video runs, the time range, people, alerts, areas, rules, and any data-quality warnings
    (repeated videos, runs that stopped early, no labelled areas). Call this first."""
    runs = one("SELECT COUNT(*) AS n, COALESCE(SUM(frames IS NULL), 0) AS unfinished FROM runs")
    pos = one("SELECT MIN(ts) AS first_ts, MAX(ts) AS last_ts, COUNT(DISTINCT substr(ts, 1, 10)) AS days, "
              "AVG(zone IS NOT NULL) AS labelled FROM positions")
    people = one(f"SELECT COUNT(*) AS n FROM (SELECT 1 FROM positions WHERE {VALID} GROUP BY run_id, person)")["n"]
    ev = q("SELECT status, COUNT(*) AS n FROM events GROUP BY status")
    dups = q("SELECT GROUP_CONCAT(id) AS ids FROM runs WHERE frames IS NOT NULL GROUP BY video, frames HAVING COUNT(*) > 1")
    warnings = [f"Runs {d['ids']} processed the same video (same length): they are repeats, so adding runs together "
                "inflates counts. Use run_id." for d in dups]
    if runs["unfinished"]:
        warnings.append(f"{runs['unfinished']} run(s) have no frame count, which means they stopped early (see list_runs).")
    if pos.get("labelled") is not None and pos["labelled"] < 0.01:
        warnings.append("Almost no positions fall inside a labelled area, so area questions will come back empty.")
    return {"runs": runs["n"], "data_from": (pos["first_ts"] or "")[:19], "data_to": (pos["last_ts"] or "")[:19],
            "days_with_data": pos["days"], "people_seen": people, "alerts_by_status": {r["status"]: r["n"] for r in ev},
            "areas": one("SELECT COUNT(*) AS n FROM areas")["n"], "rules": one("SELECT COUNT(*) AS n FROM rules")["n"],
            "warnings": warnings}


@tool
def list_runs() -> list:
    """Every video run: when its footage starts (video clock), how long it is, whether it finished, how many people it
    holds, and which other runs are repeats of the same video. People are identified by (run_id, person)."""
    people = {r["run_id"]: r["n"] for r in q(
        "SELECT run_id, COUNT(*) AS n FROM (SELECT run_id, person FROM positions GROUP BY run_id, person "
        f"HAVING COUNT(*) >= {MIN_SAMPLES}) GROUP BY run_id")}
    last = {r["run_id"]: r["last_ts"] for r in q("SELECT run_id, MAX(ts) AS last_ts FROM positions GROUP BY run_id")}
    runs = q("SELECT id, video, started_at, fps, frames FROM runs ORDER BY id")
    out = []
    for r in runs:
        same = [o["id"] for o in runs if o["id"] != r["id"] and r["frames"] is not None
                and o["video"] == r["video"] and o["frames"] == r["frames"]]
        out.append({"run_id": r["id"], "video": os.path.basename(str(r["video"]).replace("\\", "/")),
                    "footage_starts": r["started_at"], "data_to": (last.get(r["id"]) or "")[:19],
                    "finished": r["frames"] is not None,
                    "length_seconds": round(r["frames"] / r["fps"]) if r["frames"] and r["fps"] else None,
                    "people": people.get(r["id"], 0), "repeat_of_runs": same})
    return out


@tool
def list_areas() -> list:
    """The labelled areas (door, hallway, counter...) with their type, note, priority and position anchor."""
    return list(_areas().values())


@tool
def list_rules() -> list:
    """The current alert rules (where, condition, number, severity, cooldown, schedule, enabled)."""
    areas = _areas()
    out = []
    for r in q("SELECT * FROM rules ORDER BY id"):
        s = json.loads(r["schedule"] or "{}")
        out.append({"id": r["id"], "area_id": r["area_id"],
                    "area": "whole view" if r["area_id"] is None else _aname(areas, r["area_id"]),
                    "condition": r["kind"], "param": r["param"], "severity": r["severity"], "cooldown": r["cooldown"],
                    "schedule": {"days": s.get("days", []), "windows": s.get("windows", []), "outside": bool(s.get("outside"))},
                    "enabled": bool(r["enabled"])})
    return out


@tool
def list_people(start: str, end: Optional[str] = None, limit: int = 50, run_id: Optional[int] = None) -> list:
    """People seen in a period, oldest first, each with how long they were on screen and the labelled areas they used.
    start/end like 2026-10-27 or 2026-10-27 22:00. A date alone covers the whole day. run_id limits it to one run."""
    a, b = span(start, end)
    areas = _areas()
    ra, rargs = rf(run_id)
    people = q(f"""SELECT run_id, person, MIN(ts) AS first_ts, MAX(ts) AS last_ts, MAX(t) - MIN(t) AS secs FROM positions
                   WHERE ts >= ? AND ts < ? AND {VALID}{ra} GROUP BY run_id, person ORDER BY first_ts LIMIT ?""",
               (a, b, *rargs, max(1, min(int(limit), 200))))
    out = []
    for p in people:
        route = _route(p["run_id"], p["person"], a, b, areas)
        out.append({"run_id": p["run_id"], "person": p["person"], "first_seen": p["first_ts"][:19],
                    "last_seen": p["last_ts"][:19], "seconds_on_screen": round(p["secs"]),
                    "summary": " → ".join(f"{r['area']} {_dur(r['seconds'])}" for r in route) or "Not in any labeled area",
                    "areas": route})
    return out


@tool
def get_person(run_id: int, person: int) -> dict:
    """One person: when they were seen, where they started and ended, their photo path and the full area-by-area route."""
    row = one("SELECT COUNT(*) AS n, MIN(ts) AS first_ts, MAX(ts) AS last_ts, MIN(t) AS t0, MAX(t) AS t1 "
              "FROM positions WHERE run_id = ? AND person = ?", (run_id, person))
    if not row.get("n"):
        raise ToolError("No such person. Use list_people to find run_id and person.")
    first = one("SELECT x, y FROM positions WHERE run_id = ? AND person = ? ORDER BY frame LIMIT 1", (run_id, person))
    last = one("SELECT x, y FROM positions WHERE run_id = ? AND person = ? ORDER BY frame DESC LIMIT 1", (run_id, person))
    photo = one("SELECT snapshot FROM persons WHERE run_id = ? AND person = ?", (run_id, person)).get("snapshot")
    return {"run_id": run_id, "person": person, "first_seen": row["first_ts"][:19], "last_seen": row["last_ts"][:19],
            "seconds_on_screen": round(row["t1"] - row["t0"]), "photo": photo,
            "start_xy": [round(first["x"], 3), round(first["y"], 3)], "end_xy": [round(last["x"], 3), round(last["y"], 3)],
            "route": _route(run_id, person, "0000-01-01 00:00:00", "9999-12-31 23:59:59", _areas())}


@tool
def list_alerts(start: Optional[str] = None, end: Optional[str] = None, status: Optional[str] = None,
                severity: Optional[str] = None, limit: int = 50) -> list:
    """Alerts, newest first. status: new, acknowledged or false_alarm. severity: info, warning or critical.
    Each has the time on the video clock, the rule that raised it, the person photo and the full-frame image path."""
    where, args = [], []
    if start:
        a, b = span(start, end)
        where += [f"{EV_TIME} >= ?", f"{EV_TIME} < ?"]
        args += [a, b]
    if status:
        where.append("status = ?")
        args.append(status)
    if severity:
        where.append("severity = ?")
        args.append(severity)
    rows = q(f"SELECT id, {EV_TIME} AS at, type, label, severity, status, rule_id, snapshot, frame_snapshot FROM events "
             f"{'WHERE ' + ' AND '.join(where) if where else ''} ORDER BY id DESC LIMIT ?", (*args, max(1, min(int(limit), 200))))
    return [{**r, "at": str(r["at"])[:19]} for r in rows]


@tool
def who_is_in_view() -> dict:
    """Who is on camera right now (the newest video run), with the labelled area they were last in.
    Needs the backend running; otherwise it only reports the last recorded moment."""
    try:
        health = http("GET", "/api/health", timeout=3)
    except ToolError:
        health = {}
    run = one("SELECT MAX(id) AS id FROM runs")["id"]
    if run is None:
        return {"as_of": None, "people": [], "note": "No video has been processed yet."}
    last = one("SELECT MAX(t) AS t, MAX(ts) AS ts FROM positions WHERE run_id = ?", (run,))
    as_of = (last["ts"] or "")[:19]
    if not health:
        return {"run_id": run, "as_of": as_of, "people": [],
                "note": "The Argus backend is not running, so there is no live view. as_of is the last recorded moment."}
    if health.get("finished") or not health.get("online"):
        return {"run_id": run, "as_of": as_of, "people": [], "note": "The video is not playing, so nobody is in view."}
    areas = _areas()
    rows = q(f"""SELECT person, first_ts, zone FROM (
                   SELECT person, zone, MIN(ts) OVER (PARTITION BY person) AS first_ts, MAX(t) OVER (PARTITION BY person) AS lt,
                          ROW_NUMBER() OVER (PARTITION BY person ORDER BY frame DESC) AS rn
                   FROM positions WHERE run_id = ? AND {VALID}) WHERE rn = 1 AND lt >= ? - 1.5""", (run, last["t"] or 0))
    return {"run_id": run, "as_of": as_of,
            "people": [{"person": r["person"], "first_seen": r["first_ts"][:19], "area": _aname(areas, r["zone"])} for r in rows]}


# ---------------------------------------------------------------- compute tools
@tool
def count_people(start: str, end: Optional[str] = None, run_id: Optional[int] = None) -> dict:
    """How many different people were seen in ANY window, with the most in view at once and the average time on screen.
    Use this for questions like 'how many people between 10:45 and 11:00' (start 2026-10-27 10:45, end 2026-10-27 11:00)."""
    a, b = span(start, end)
    m = _metrics(a, b, run_id)
    ra, rargs = rf(run_id)
    by_run = q(f"SELECT run_id, COUNT(DISTINCT person) AS people FROM positions WHERE ts >= ? AND ts < ? AND {VALID}{ra} "
               f"GROUP BY run_id ORDER BY run_id", (a, b, *rargs))
    return _with_note({"from": a, "to": b, "people": m["people"], "most_at_once": m["most_at_once"],
                       "average_seconds_on_screen": m["average_seconds_on_screen"], "by_run": by_run},
                      _runs_note(a, b, run_id))


@tool
def hourly_counts(date: str, run_id: Optional[int] = None) -> dict:
    """For one day: per hour, how many different people were seen and the most in view at once."""
    a, b = span(date)
    people, peak = _hourly(a, b, run_id)
    return _with_note({"date": a[:10], "hours": [{"hour": f"{h}:00", "people": people.get(h, 0), "most_at_once": peak.get(h, 0)}
                                                 for h in sorted(set(people) | set(peak))]}, _runs_note(a, b, run_id))


@tool
def area_dwell(start: str, end: Optional[str] = None, run_id: Optional[int] = None) -> dict:
    """Time spent in each labelled area during a period: total seconds and number of different people.
    Time outside every labelled area is not counted."""
    a, b = span(start, end)
    return _with_note({"from": a, "to": b, "areas": _dwell(a, b, _areas(), run_id)}, _runs_note(a, b, run_id))


@tool
def daily_briefing(date: str, open_time: Optional[str] = None, close_time: Optional[str] = None,
                   run_id: Optional[int] = None) -> dict:
    """All the facts for a daily briefing, for one day: people, busiest moment, alerts, areas, longest stays and, if
    opening hours are given (08:00 and 18:00), people outside those hours. Write the briefing from these facts only."""
    a, b = span(date)
    areas = _areas()
    ra, rargs = rf(run_id)
    m = _metrics(a, b, run_id)
    people, peak = _hourly(a, b, run_id)
    busiest = max(people, key=people.get) if people else None
    moment = one(f"SELECT MIN(ts) AS ts, COUNT(DISTINCT person) AS c FROM positions WHERE ts >= ? AND ts < ? AND {VALID}{ra} "
                 f"GROUP BY run_id, frame ORDER BY c DESC, ts LIMIT 1", (a, b, *rargs))
    where, eargs = _ev_where(a, b, run_id)
    out = {"date": a[:10], "run_id": run_id, **m,
           "peak_moment": {"people": moment.get("c", 0), "at": (moment.get("ts") or "")[:19]} if moment else None,
           "busiest_hour": f"{busiest}:00 ({people[busiest]} people)" if busiest else None,
           "alerts_by_type": {r["type"]: r["n"] for r in q(f"SELECT type, COUNT(*) AS n FROM events WHERE {where} GROUP BY type", eargs)},
           "alerts_by_status": {r["status"]: r["n"] for r in q(f"SELECT status, COUNT(*) AS n FROM events WHERE {where} GROUP BY status", eargs)},
           "top_areas": _dwell(a, b, areas, run_id)[:5],
           "longest_stays": [{"run_id": r["run_id"], "person": r["person"], "seconds": round(r["s"]), "from": r["f"][:19]} for r in q(
               f"SELECT run_id, person, MAX(t) - MIN(t) AS s, MIN(ts) AS f FROM positions WHERE ts >= ? AND ts < ? AND {VALID}{ra} "
               f"GROUP BY run_id, person ORDER BY s DESC LIMIT 3", (a, b, *rargs))]}
    notes = [n for n in (_runs_note(a, b, run_id),) if n]
    if open_time and close_time:
        rows = _outside(a, b, open_time, close_time, run_id)
        out["outside_opening_hours"] = {"hours": f"{open_time} to {close_time}", "people": len(rows),
                                        "first": rows[0]["first_ts"][:19] if rows else None, "last": rows[-1]["last_ts"][:19] if rows else None}
    else:
        notes.append("No opening hours given, so after-hours presence was not checked.")
    if m["people"] == 0:
        notes.append("No people were recorded on this date.")
    if notes:
        out["note"] = " ".join(notes)
    return out


@tool
def compare_periods(a_start: str, b_start: str, a_end: Optional[str] = None, b_end: Optional[str] = None,
                    run_a: Optional[int] = None, run_b: Optional[int] = None) -> dict:
    """Compare two periods (for example two days): people, most at once, average time on screen, alerts, false alarms and
    busiest areas, with the difference B minus A. A date alone covers the whole day. To compare two video runs, give the
    same date for both and set run_a and run_b."""
    (a1, a2), (b1, b2) = span(a_start, a_end), span(b_start, b_end)
    areas = _areas()
    A, B = _metrics(a1, a2, run_a), _metrics(b1, b2, run_b)
    out = {"a": {"period": f"{a1} to {a2}", "run_id": run_a, **A, "top_areas": _dwell(a1, a2, areas, run_a)[:3]},
           "b": {"period": f"{b1} to {b2}", "run_id": run_b, **B, "top_areas": _dwell(b1, b2, areas, run_b)[:3]},
           "b_minus_a": {k: B[k] - A[k] for k in A}}
    notes = [n for n in (_runs_note(a1, a2, run_a), _runs_note(b1, b2, run_b)) if n]
    return _with_note(out, " ".join(dict.fromkeys(notes)))


@tool
def find_anomalies(date: str, baseline_days: int = 7, open_time: Optional[str] = None, close_time: Optional[str] = None) -> dict:
    """What looked unusual on a day: hours busier than the earlier days (mean + 2 standard deviations), alert volume against
    the baseline, and anyone outside opening hours. With fewer than 3 earlier days it says the baseline is too thin."""
    a, b = span(date)
    day = a[:10]
    today, _ = _hourly(a, b)
    prev = [r["d"] for r in q("SELECT DISTINCT substr(ts, 1, 10) AS d FROM positions WHERE substr(ts, 1, 10) < ? ORDER BY d DESC LIMIT ?",
                              (day, max(1, min(int(baseline_days), 60))))]
    out = {"date": day, "baseline_days_used": len(prev), "unusual_hours": [], "alert_volume": None}
    if len(prev) < 3:
        out["note"] = (f"Only {len(prev)} earlier day(s) of data, which is too few to call any hour unusual. "
                       "Process more days (different START dates) to build a baseline.")
    else:
        hist = {}
        for d in prev:
            p, _ = _hourly(*span(d))
            for h in range(24):
                hist.setdefault(f"{h:02d}", []).append(p.get(f"{h:02d}", 0))
        for h, n in sorted(today.items()):
            vals = hist.get(h, [0] * len(prev))
            mean = sum(vals) / len(vals)
            sd = (sum((v - mean) ** 2 for v in vals) / len(vals)) ** 0.5
            if n > mean + 2 * sd and n >= mean + 2:
                out["unusual_hours"].append({"hour": f"{h}:00", "people": n, "usual_average": round(mean, 1), "usual_spread": round(sd, 1)})
        counts = [one(f"SELECT COUNT(*) AS n FROM events WHERE {EV_TIME} >= ? AND {EV_TIME} < ?", span(d))["n"] for d in prev]
        n_today = one(f"SELECT COUNT(*) AS n FROM events WHERE {EV_TIME} >= ? AND {EV_TIME} < ?", (a, b))["n"]
        out["alert_volume"] = {"today": n_today, "usual_average": round(sum(counts) / len(counts), 1)}
    if open_time and close_time:
        rows = _outside(a, b, open_time, close_time)
        out["outside_opening_hours"] = [{"run_id": r["run_id"], "person": r["person"], "from": r["first_ts"][:19], "to": r["last_ts"][:19]} for r in rows]
    return out


@tool
def closing_check(at: Optional[str] = None, lookback_minutes: int = 30, run_id: Optional[int] = None) -> dict:
    """'Anyone still inside?' at a moment (default: the newest data of the newest run). Lists people in view, and people last
    seen recently somewhere other than a door. This is an estimate: until door tripwire lines exist, Argus cannot see people leave."""
    if run_id is None and at is None:
        run_id = one("SELECT MAX(id) AS id FROM runs")["id"]
    ra, rargs = rf(run_id)
    last = one(f"SELECT MAX(ts) AS ts FROM positions WHERE 1 = 1{ra}", rargs)["ts"]
    if not last:
        return {"note": "No data yet."}
    at_s = _parse(at) if at else last[:19]
    since = (datetime.strptime(at_s, FMT) - timedelta(minutes=max(1, int(lookback_minutes)))).strftime(FMT)
    in_view_from = (datetime.strptime(at_s, FMT) - timedelta(seconds=2)).strftime(FMT)
    areas = _areas()
    rows = q(f"""SELECT run_id, person, last_ts, zone FROM (
                   SELECT run_id, person, ts AS last_ts, zone, ROW_NUMBER() OVER (PARTITION BY run_id, person ORDER BY frame DESC) AS rn
                   FROM positions WHERE ts <= ? AND ts >= ? AND {VALID}{ra}) WHERE rn = 1 ORDER BY last_ts""",
             (at_s + ".999", since, *rargs))
    in_view, unsure, left = [], [], []
    for r in rows:
        info = {"run_id": r["run_id"], "person": r["person"], "last_seen": r["last_ts"][:19], "area": _aname(areas, r["zone"])}
        if r["last_ts"] >= in_view_from:
            in_view.append(info)
        elif areas.get(r["zone"], {}).get("type") == "door":
            left.append(info)
        else:
            unsure.append(info)
    return {"at": at_s, "run_id": run_id, "lookback_minutes": lookback_minutes, "in_view_now": in_view,
            "last_seen_away_from_a_door": unsure, "last_seen_at_a_door_probably_left": left,
            "note": "An estimate. Someone last seen away from a door may have left the camera view without being seen leaving."}


# ---------------------------------------------------------------- propose tools (nothing changes until the user approves)
def _propose(body):
    p = http("POST", "/api/mcp/proposals", body)
    return {"proposal_id": p["id"], "status": "pending", "summary": p["summary"],
            "message": "Saved as a proposal. Nothing has changed: the user must approve it in the Argus dashboard (/approvals)."}


@tool
def list_proposals(status: str = "pending") -> list:
    """Rule proposals and what happened to them. status: pending, approved, rejected, failed or all."""
    return http("GET", f"/api/proposals?status={status}")


@tool
def propose_rule(condition: str, area_id: Optional[int] = None, param: Optional[float] = None, severity: str = "warning",
                 cooldown: int = 60, schedule: Optional[dict] = None, reason: str = "") -> dict:
    """Propose a NEW alert rule. condition: presence | loiter (param = seconds) | crowd (param = people, at least 2) |
    unattended (param = minutes). area_id from list_areas, or null for the whole view. severity: info | warning | critical.
    schedule: {"days": [0-6, Monday is 0, empty = every day], "windows": [["22:00", "06:00"]]}; a window may cross midnight.
    Do not set "outside": it is not supported. For after-hours, give the closed hours as the window.
    'More than 3 people' means crowd with param 4. Say in `reason` why this rule helps."""
    return _propose({"action": "create", "reason": reason, "rule": {
        "area_id": area_id, "condition": condition, "param": param, "severity": severity, "cooldown": cooldown,
        "schedule": schedule or {}, "enabled": True}})


@tool
def propose_rule_change(rule_id: int, changes: dict, reason: str = "") -> dict:
    """Propose changing an existing rule. changes may hold: area_id, condition, param, severity, cooldown, schedule, enabled.
    To pause or resume a rule, use {"enabled": false} or {"enabled": true}. Get rule ids from list_rules."""
    return _propose({"action": "update", "rule_id": rule_id, "changes": changes, "reason": reason})


@tool
def propose_rule_delete(rule_id: int, reason: str = "") -> dict:
    """Propose deleting a rule. Prefer pausing it with propose_rule_change unless it is clearly wrong."""
    return _propose({"action": "delete", "rule_id": rule_id, "reason": reason})


if __name__ == "__main__":
    mcp.run()

# Claude Desktop config (claude_desktop_config.json), replacing the old "argus" entry:
# {"mcpServers": {"argus": {"command": "python",
#   "args": ["C:\\Users\\LENOVO\\Downloads\\argus-dashboard-v2\\backend\\argus_mcp.py"],
#   "env": {"ARGUS_MCP_TOKEN": "pick-a-long-random-string"}}}}
# Start the backend with the SAME ARGUS_MCP_TOKEN set (and, optionally, ARGUS_ADMIN_TOKEN for approvals).
