"""Argus proposals: what the AI suggests, what YOU decide, and the audit log of every AI tool call.

The AI side (argus_mcp.py) can only reach create() and audit(), through /api/mcp/* routes.
approve() and reject() are reachable only from the dashboard routes in app.py, and those routes refuse any request
that carries the MCP server's token header. Approving applies the change through rules.py, so every proposal is
validated twice: when it is saved and again when it is applied (the area or rule may have changed in between).
"""
import json
import sqlite3
import threading
import time

import rules as R

STATUSES = ("pending", "approved", "rejected", "failed")
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL, action TEXT, rule_id INTEGER,
    payload TEXT,                         -- JSON: {"rule": {...}} for create, {"changes": {...}} for update
    summary TEXT, reason TEXT, status TEXT DEFAULT 'pending', decided REAL,
    result TEXT);                         -- JSON: the saved rule, or {"error": ...} when it failed
CREATE TABLE IF NOT EXISTS mcp_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, tool TEXT, args TEXT, ok INTEGER, note TEXT, ms INTEGER);
"""
_lock = threading.Lock()


def _conn(path):
    c = sqlite3.connect(path, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def init_proposals(path):
    with _conn(path) as c:
        c.executescript(SCHEMA)


def _row(r):
    d = dict(r)
    d["payload"] = json.loads(d["payload"] or "{}")
    d["result"] = json.loads(d["result"]) if d.get("result") else None
    return d


def _names(path):
    with _conn(path) as c:
        return {r["id"]: r["name"] for r in c.execute("SELECT id, name FROM areas")}


def _check_area(path, area_id):
    if area_id is not None and area_id not in _names(path):
        raise ValueError(f"area {area_id} does not exist")


def describe(rule, names):
    """One plain sentence for the approval screen."""
    area = rule.get("area_id")
    where = "the whole view" if area is None else f"'{names.get(area, f'area {area}')}'"
    kind, n = rule["condition"], rule.get("param")
    if kind == "presence":
        what = f"anyone is in {where}"
    elif kind == "loiter":
        what = f"a person stays over {n:g} s in {where}"
    elif kind == "crowd":
        what = f"{int(n)} or more people are in {where}"
    else:
        what = f"nobody is in {where} for {n:g} min"
    s = rule.get("schedule") or {}
    days = ", ".join(DAYS[d] for d in s.get("days", [])) or "every day"
    wins = ", ".join(f"{a}-{b}" for a, b in s.get("windows", []))
    when = f"{days}, {wins}" if wins else days
    return f"Alert when {what} ({rule['severity']}, {when}, cooldown {rule['cooldown']} s)"


def create(path, body):
    """Validate and store a pending proposal. Raises ValueError with a readable message."""
    action = body.get("action")
    reason = str(body.get("reason") or "")[:500]
    names = _names(path)
    rid = None
    if action == "create":
        raw = body.get("rule") or {}
        _no_outside(raw)
        v = R.clean(raw)
        _check_area(path, v["area_id"])
        payload, summary = {"rule": v}, "New rule: " + describe(v, names)
    elif action in ("update", "delete"):
        try:
            rid = int(body.get("rule_id"))
        except (TypeError, ValueError):
            raise ValueError("rule_id is required")
        cur = R.get_rule(path, rid)
        if not cur:
            raise ValueError(f"rule {rid} does not exist")
        if action == "delete":
            payload, summary = {}, f"Delete rule #{rid}: " + describe(cur, names)
        else:
            changes = body.get("changes")
            if not isinstance(changes, dict) or not changes:
                raise ValueError("changes must be a non-empty object")
            _no_outside(changes)
            v = R.clean({**cur, **changes})
            _check_area(path, v["area_id"])
            diff = "; ".join(f"{k}: {cur.get(k)} -> {v.get(k)}" for k in changes if k in v and v[k] != cur.get(k))
            payload = {"changes": changes}
            summary = f"Change rule #{rid} ({diff or 'no visible change'}). Result: " + describe(v, names)
    else:
        raise ValueError("action must be create, update or delete")
    with _conn(path) as c:
        cur_ = c.execute("INSERT INTO proposals (created, action, rule_id, payload, summary, reason) VALUES (?,?,?,?,?,?)",
                         (time.time(), action, rid, json.dumps(payload), summary, reason))
        return _row(c.execute("SELECT * FROM proposals WHERE id=?", (cur_.lastrowid,)).fetchone())


def _no_outside(d):
    if ((d.get("schedule") or {}).get("outside")):
        raise ValueError("schedule.outside is not supported yet: give the hours the rule should run as a window "
                         "(for example [['22:00', '06:00']] for after hours)")


def list_proposals(path, status="pending"):
    if status != "all" and status not in STATUSES:
        raise ValueError(f"status must be one of {list(STATUSES) + ['all']}")
    sql = "SELECT * FROM proposals" + ("" if status == "all" else " WHERE status=?") + " ORDER BY id DESC LIMIT 200"
    with _conn(path) as c:
        return [_row(r) for r in c.execute(sql, () if status == "all" else (status,))]


def _decide(path, pid, status, result=None):
    with _conn(path) as c:
        c.execute("UPDATE proposals SET status=?, decided=?, result=? WHERE id=?",
                  (status, time.time(), json.dumps(result, default=str) if result is not None else None, pid))
        return _row(c.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone())


def _pending(path, pid):
    with _conn(path) as c:
        r = c.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()
    if not r:
        raise LookupError("unknown proposal")
    if r["status"] != "pending":
        raise ValueError(f"this proposal is already {r['status']}")
    return _row(r)


def approve(path, pid):
    """Apply a pending proposal. Returns the proposal with status 'approved', or 'failed' and the reason."""
    with _lock:
        p = _pending(path, pid)
        try:
            if p["action"] == "create":
                rule = p["payload"]["rule"]
                _check_area(path, rule.get("area_id"))
                out = R.add_rule(path, rule, 1)
            else:
                cur = R.get_rule(path, p["rule_id"])
                if not cur:
                    raise ValueError(f"rule {p['rule_id']} no longer exists")
                if p["action"] == "delete":
                    R.delete_rule(path, p["rule_id"])
                    out = {"deleted": p["rule_id"]}
                else:
                    v = R.clean({**cur, **p["payload"]["changes"]})
                    _check_area(path, v["area_id"])
                    out = R.update_rule(path, p["rule_id"], p["payload"]["changes"])
        except (ValueError, KeyError, sqlite3.Error) as e:
            return _decide(path, pid, "failed", {"error": str(e)})
        return _decide(path, pid, "approved", out)


def reject(path, pid):
    with _lock:
        _pending(path, pid)
        return _decide(path, pid, "rejected")


# ---------- audit log of every AI tool call ----------
def audit(path, e):
    with _conn(path) as c:
        c.execute("INSERT INTO mcp_audit (ts, tool, args, ok, note, ms) VALUES (?,?,?,?,?,?)",
                  (time.time(), str(e.get("tool", ""))[:80], json.dumps(e.get("args"), default=str)[:2000],
                   int(bool(e.get("ok"))), str(e.get("note", ""))[:300], int(e.get("ms") or 0)))


def list_audit(path, limit=100):
    with _conn(path) as c:
        return [dict(r) for r in c.execute("SELECT * FROM mcp_audit ORDER BY id DESC LIMIT ?", (max(1, min(int(limit), 500)),))]
