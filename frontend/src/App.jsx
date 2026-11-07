import { createContext, useContext, useEffect, useRef, useState } from "react";

// Camera 1 is real. Cameras 2-6 are demo mirrors: they show Camera 1's video and data (src: 1).
const CAMS = [{ id: 1, src: 1, name: "Camera 1" }, ...[2, 3, 4, 5, 6].map(id => ({ id, src: 1, name: `Camera ${id}` }))];
const W = 16, H = 9; // shapes live in a 16x9 space so circles stay round
const load = (k, d) => { try { return JSON.parse(localStorage.getItem(k)) ?? d; } catch { return d; } };
const save = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} };
const ago = ts => {
  const s = Math.round((Date.now() - ts) / 1000);
  return s < 10 ? "Just now" : s < 60 ? `${s} seconds ago` : s < 3600 ? `${Math.floor(s / 60)} minutes ago`
    : new Date(ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
};
const api = (url, method = "GET", body) =>
  fetch(url, { method, headers: { "Content-Type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body) })
    .then(r => { if (!r.ok) throw new Error(r.status); return r.json(); });

// polls a JSON endpoint; pass url=null to switch it off
function useJson(url, ms) {
  const [data, setData] = useState(null);
  useEffect(() => {
    if (!url) { setData(null); return; }
    let on = true;
    const go = () => api(url).then(d => on && setData(d)).catch(() => on && setData(null));
    go();
    const t = ms ? setInterval(go, ms) : null;
    return () => { on = false; if (t) clearInterval(t); };
  }, [url, ms]);
  return data;
}

const Icon = ({ n }) => (
  <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    {{
      grid: <><rect x="3" y="3" width="7" height="7" rx="1.5" /><rect x="14" y="3" width="7" height="7" rx="1.5" /><rect x="3" y="14" width="7" height="7" rx="1.5" /><rect x="14" y="14" width="7" height="7" rx="1.5" /></>,
      cam: <><path d="M3 8a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" /><path d="m15 11 6-3v8l-6-3" /></>,
      bell: <><path d="M6 9a6 6 0 1 1 12 0c0 6 2 7 2 7H4s2-1 2-7" /><path d="M10 20a2 2 0 0 0 4 0" /></>,
      out: <><path d="M9 4H5a1 1 0 0 0-1 1v14a1 1 0 0 0 1 1h4" /><path d="m16 8 4 4-4 4M20 12H9" /></>,
    }[n]}
  </svg>
);

function useBackend() {
  const [health, setHealth] = useState(null);
  const [events, setEvents] = useState([]);
  useEffect(() => {
    const load = () => api("/api/health").then(setHealth).catch(() => setHealth(null));
    load();
    const t = setInterval(load, 2000);
    const es = new EventSource("/api/events/stream");        // new alerts arrive instantly
    es.onmessage = m => setEvents(l => [JSON.parse(m.data), ...l].slice(0, 50));
    return () => { clearInterval(t); es.close(); };
  }, []);
  useEffect(() => { if (health) api("/api/events").then(setEvents).catch(() => {}); }, [!!health]);
  const ack = ids => {
    setEvents(l => l.map(e => (ids.includes(e.id) ? { ...e, status: "acknowledged" } : e)));
    ids.forEach(id => api(`/api/events/${id}/ack`, "POST", {}).catch(() => {}));
  };
  return { health, setHealth, events, ack };
}

// One video connection from the backend, copied into every tile (browsers allow only ~6 per site).
const Src = createContext({ img: { current: null }, live: false });

const Shape = ({ s, draft }) => {
  const p = { className: draft ? "zone draft" : "zone" };
  if (s.t === "circle") return <circle cx={s.cx} cy={s.cy} r={s.r} {...p} />;
  if (s.t === "rect") return <rect x={s.x} y={s.y} width={s.w} height={s.h} {...p} />;
  return <polygon points={s.pts.map(q => q.join(",")).join(" ")} {...p} />;
};

const areaShape = a => ({ t: "poly", pts: a.polygon.map(([x, y]) => [x * W, y * H]) });
const centre = a => [a.polygon.reduce((s, p) => s + p[0], 0) / a.polygon.length * W, a.polygon.reduce((s, p) => s + p[1], 0) / a.polygon.length * H];
const AreaNames = ({ areas }) => areas.map(a => {
  const [cx, cy] = centre(a);
  return <text key={a.id} x={cx} y={cy} textAnchor="middle" style={{ fill: "#fff", stroke: "#000", strokeWidth: 0.04, paintOrder: "stroke", fontSize: 0.38, pointerEvents: "none" }}>{a.name}</text>;
});

function Feed({ zones, hot, children }) {
  const { img, live } = useContext(Src);
  const cv = useRef();
  useEffect(() => {
    let raf, t = 0;
    const tick = ts => {
      raf = requestAnimationFrame(tick);
      if (ts - t < 66) return;
      t = ts;
      const i = img.current, c = cv.current;
      if (i?.naturalWidth && c) c.getContext("2d").drawImage(i, 0, 0, c.width, c.height);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [img]);
  return (
    <div className={"feed" + (hot ? " hot" : "")}>
      <canvas ref={cv} width="640" height="360" />
      {!live && <span className="nofeed">No video</span>}
      <svg viewBox={`0 0 ${W} ${H}`} className="shapes">{zones.map((s, i) => <Shape key={i} s={s} />)}</svg>
      {children}
    </div>
  );
}

/* ---------------------------------------------------------------- Alerts: rules */

const CONDS = [["presence", "Someone is there"], ["loiter", "Someone stays too long"], ["crowd", "Too many people"], ["unattended", "Nobody there for too long"]];
const SEVS = ["info", "warning", "critical"];
const SEV_COLOR = { info: "#3b82f6", warning: "#f59e0b", critical: "#ef4444" };
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const PARAM = { loiter: ["Seconds", 10], crowd: ["People", 3], unattended: ["Minutes", 5] };
const blankRule = () => ({ area_id: "", condition: "presence", param: "", severity: "warning", cooldown: 60, mode: "always", from: "08:00", to: "18:00", days: [], enabled: true });

const toBody = f => ({
  area_id: f.area_id === "" ? null : Number(f.area_id),
  condition: f.condition,
  param: f.condition === "presence" ? null : Number(f.param || PARAM[f.condition][1]),
  severity: f.severity,
  cooldown: Number(f.cooldown) || 0,
  enabled: f.enabled,
  schedule: {
    days: f.days,
    windows: f.mode === "always" ? [] : f.mode === "during" ? [[f.from, f.to]] : [[f.to, f.from]],   // "outside hours" = from closing to opening
    outside: f.mode === "outside",
  },
});
const fromRule = r => {
  const w = r.schedule.windows[0];
  const mode = !w ? "always" : r.schedule.outside ? "outside" : "during";
  return {
    id: r.id, area_id: r.area_id ?? "", condition: r.condition, param: r.param ?? "", severity: r.severity, cooldown: r.cooldown,
    mode, from: !w ? "08:00" : mode === "outside" ? w[1] : w[0], to: !w ? "18:00" : mode === "outside" ? w[0] : w[1],
    days: r.schedule.days, enabled: r.enabled,
  };
};
const describe = (r, areas) => {
  const where = r.area_id == null ? "Whole view" : areas.find(a => a.id === r.area_id)?.name ?? "Deleted area";
  const what = { presence: "someone is there", loiter: `someone stays over ${r.param} s`, crowd: `${r.param} or more people`, unattended: `nobody for ${r.param} min` }[r.condition];
  const w = r.schedule.windows[0];
  const when = !w ? "any time" : r.schedule.outside ? `outside ${w[1]} to ${w[0]}` : `${w[0]} to ${w[1]}`;
  const days = r.schedule.days.length && r.schedule.days.length < 7 ? `, ${r.schedule.days.map(d => DAYS[d]).join(" ")}` : "";
  return { title: `${where}: ${what}`, detail: `${r.severity}, ${when}${days}, cooldown ${r.cooldown} s` };
};

const RULE_CSS = `
.cam-layout { align-items: start; }
@media (max-width: 900px) { .cam-layout { grid-template-columns: minmax(0, 1fr) !important; } }

.rf { display: flex; flex-direction: column; gap: 18px; }
.rf-field { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.rf-label { font-size: 13px; font-weight: 600; }
.rf-help { font-size: 12.5px; opacity: .7; margin: 0; }
.rf-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 12px; }

.rf select, .rf input:not([type=checkbox]):not([type=radio]) {
  width: 100%; box-sizing: border-box; min-height: 44px; padding: 8px 12px; font: inherit; font-size: 16px;
  color: inherit; background-color: rgba(128,128,128,.10); border: 1px solid rgba(128,128,128,.35); border-radius: 10px;
}
.rf select {
  appearance: none; -webkit-appearance: none; padding-right: 38px; cursor: pointer;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='16' height='16' viewBox='0 0 24 24' fill='none' stroke='%23888' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='m6 9 6 6 6-6'/%3E%3C/svg%3E");
  background-repeat: no-repeat; background-position: right 12px center;
}
.rf select option { color: #111; background: #fff; }
.rf select:focus-visible, .rf input:focus-visible, .rf button:focus-visible { outline: 2px solid #ffc800; outline-offset: 2px; }

.rf-unit { position: relative; }
.rf-unit input { padding-right: 76px !important; }
.rf-unit span { position: absolute; right: 12px; top: 50%; transform: translateY(-50%); font-size: 13px; opacity: .65; pointer-events: none; }

.rf-conds { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 8px; }
.rf-cond { text-align: left; padding: 10px 12px; min-height: 44px; font: inherit; color: inherit; cursor: pointer;
  background: rgba(128,128,128,.08); border: 1px solid rgba(128,128,128,.35); border-radius: 10px; }
.rf-cond b { display: block; font-size: 14px; }
.rf-cond small { display: block; font-size: 12px; opacity: .7; margin-top: 2px; }
.rf-cond.on, .rf-seg button.on, .rf-chip.on { border-color: #ffc800; background: rgba(255,200,0,.16); }

.rf-seg { display: flex; flex-wrap: wrap; gap: 6px; }
.rf-seg button, .rf-chip { flex: 1 1 auto; min-height: 44px; padding: 8px 14px; font: inherit; font-size: 14px; color: inherit; cursor: pointer;
  display: inline-flex; align-items: center; justify-content: center; gap: 8px;
  background: rgba(128,128,128,.08); border: 1px solid rgba(128,128,128,.35); border-radius: 10px; }
.rf-seg button i { width: 9px; height: 9px; border-radius: 50%; display: inline-block; }

.rf-days { display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 6px; }
.rf-chip { flex: none; padding: 8px 0; }
.rf-presets { display: flex; flex-wrap: wrap; gap: 6px; }
.rf-presets button { min-height: 34px; padding: 4px 12px; font: inherit; font-size: 13px; color: inherit; cursor: pointer;
  background: none; border: 1px solid rgba(128,128,128,.35); border-radius: 999px; }
.rf-presets button.on { border-color: #ffc800; background: rgba(255,200,0,.16); }

.rf-sum { padding: 10px 12px; border-radius: 10px; border: 1px dashed rgba(128,128,128,.5); font-size: 13.5px; }
.rf-sum small { display: block; opacity: .7; margin-top: 2px; }
.rf-actions { position: sticky; bottom: 0; display: flex; gap: 8px; padding: 12px 0 2px; margin-top: 2px;
  background: linear-gradient(to top, var(--rf-bg, Canvas) 70%, transparent); }
.rf-actions button { min-height: 44px; flex: 1; }
@media (min-width: 600px) { .rf-actions { position: static; background: none; } .rf-actions button { flex: none; } }

.rf-rules { list-style: none; margin: 12px 0 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
.rf-rule { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px 12px; padding: 10px 12px;
  border: 1px solid rgba(128,128,128,.3); border-radius: 10px; }
.rf-rule .txt { flex: 1 1 220px; min-width: 0; overflow-wrap: anywhere; }
.rf-rule .txt small { display: block; opacity: .7; }
.rf-rule .btns { display: flex; gap: 4px; flex-wrap: wrap; }
.rf-rule .btns button { min-height: 36px; }

.rf-toggle { display: flex; align-items: center; gap: 10px; min-height: 44px; cursor: pointer; font-size: 14px; }
.rf-toggle input { width: 20px; height: 20px; accent-color: #ffc800; flex: none; }
.rf-inline { display: flex; flex-wrap: wrap; gap: 8px; }
.rf-inline button { min-height: 40px; }
.draw { touch-action: none; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 240px), 1fr)); gap: 12px; }
.chart-box { width: 100%; min-width: 0; }
`;

const Seg = ({ label, value, onChange, options }) => (
  <div className="rf-seg" role="radiogroup" aria-label={label}>
    {options.map(([id, text, color]) => (
      <button key={id} type="button" role="radio" aria-checked={value === id} className={value === id ? "on" : ""} onClick={() => onChange(id)}>
        {color && <i style={{ background: color }} />}{text}
      </button>
    ))}
  </div>
);

const CONDITION_HINT = {
  presence: "Anyone is in the area",
  loiter: "Someone stays too long",
  crowd: "Too many people at once",
  unattended: "Nobody there for too long",
};
const PRESETS = [["Every day", []], ["Weekdays", [0, 1, 2, 3, 4]], ["Weekends", [5, 6]]];
const sameDays = (a, b) => a.length === b.length && a.every((v, i) => v === b[i]);

// Per-camera tab: rules that raise alerts. Rules pick from the areas you drew in Labels (or the whole view).
function Alerts({ cid }) {
  const base = `/api/cameras/${cid}`;
  const [rules, setRules] = useState([]);
  const [areas, setAreas] = useState([]);
  const [form, setForm] = useState(null);
  const [msg, setMsg] = useState("");
  const reload = () => {
    api(`${base}/rules`).then(setRules).catch(() => setMsg("Can't reach the backend."));
    api(`${base}/areas`).then(setAreas).catch(() => {});
  };
  useEffect(() => { reload(); }, [cid]);

  const up = patch => setForm(f => ({ ...f, ...patch }));
  const set = k => e => up({ [k]: e.target.value });
  const setDays = days => up({ days });
  const toggleDay = d => setDays(form.days.includes(d) ? form.days.filter(x => x !== d) : [...form.days, d].sort());

  const saveRule = () => {
    if (form.condition !== "presence" && !(Number(form.param || PARAM[form.condition][1]) > 0)) return setMsg(`${PARAM[form.condition][0]} must be more than 0.`);
    if (form.mode !== "always" && (!form.from || !form.to)) return setMsg("Set both times.");
    if (form.mode !== "always" && form.from === form.to) return setMsg("The two times must be different.");
    const body = toBody(form);
    (form.id ? api(`${base}/rules/${form.id}`, "PUT", body) : api(`${base}/rules`, "POST", body))
      .then(() => { setForm(null); setMsg(""); reload(); })
      .catch(() => setMsg("Could not save the rule. Check the number and the times."));
  };
  const flip = r => api(`${base}/rules/${r.id}`, "PUT", { enabled: !r.enabled }).then(reload).catch(() => {});
  const remove = id => { if (window.confirm("Delete this rule?")) api(`${base}/rules/${id}`, "DELETE").then(reload).catch(() => {}); };
  const picked = form && form.area_id !== "" ? areas.find(a => a.id === Number(form.area_id)) : null;
  const preview = form ? describe({ ...toBody(form), id: 0 }, areas) : null;

  return (
    <div className="cam-layout">
      <Feed zones={areas.map(areaShape)} hot={false}>
        <svg viewBox={`0 0 ${W} ${H}`} className="shapes">
          {picked && <Shape s={areaShape(picked)} draft />}
          <AreaNames areas={areas} />
        </svg>
      </Feed>
      <div className="card">
        {form ? (
          <div className="rf">
            <h3 style={{ margin: 0 }}>{form.id ? "Edit rule" : "New rule"}</h3>

            <div className="rf-field">
              <label className="rf-label" htmlFor="rf-where">Where</label>
              <select id="rf-where" value={form.area_id} onChange={set("area_id")}>
                <option value="">Whole view</option>
                {areas.map(a => <option key={a.id} value={a.id}>{a.name} ({a.type})</option>)}
              </select>
              {areas.length === 0
                ? <p className="rf-help">No areas yet. Draw some in Labels to pick a specific place.</p>
                : <p className="rf-help">The chosen area is outlined on the video.</p>}
            </div>

            <div className="rf-field">
              <span className="rf-label" id="rf-when-l">Alert when</span>
              <div className="rf-conds" role="radiogroup" aria-labelledby="rf-when-l">
                {CONDS.map(([id, l]) => (
                  <button key={id} type="button" role="radio" aria-checked={form.condition === id}
                    className={"rf-cond" + (form.condition === id ? " on" : "")} onClick={() => up({ condition: id, param: "" })}>
                    <b>{l}</b><small>{CONDITION_HINT[id]}</small>
                  </button>
                ))}
              </div>
            </div>

            {form.condition !== "presence" && (
              <div className="rf-field">
                <label className="rf-label" htmlFor="rf-param">{PARAM[form.condition][0]}</label>
                <div className="rf-unit">
                  <input id="rf-param" type="number" inputMode="decimal" min="0" step="any" value={form.param} onChange={set("param")} placeholder={String(PARAM[form.condition][1])} />
                  <span>{PARAM[form.condition][0].toLowerCase()}</span>
                </div>
              </div>
            )}

            <div className="rf-field">
              <span className="rf-label">How serious</span>
              <Seg label="Severity" value={form.severity} onChange={v => up({ severity: v })}
                options={SEVS.map(s => [s, s[0].toUpperCase() + s.slice(1), SEV_COLOR[s]])} />
            </div>

            <div className="rf-field">
              <span className="rf-label">Time</span>
              <Seg label="Time window" value={form.mode} onChange={v => up({ mode: v })}
                options={[["always", "Always"], ["during", "Only during hours"], ["outside", "Outside opening hours"]]} />
              {form.mode !== "always" && (
                <div className="rf-row" style={{ marginTop: 6 }}>
                  <div className="rf-field">
                    <label className="rf-label" htmlFor="rf-from">{form.mode === "outside" ? "Opens" : "From"}</label>
                    <input id="rf-from" type="time" value={form.from} onChange={set("from")} />
                  </div>
                  <div className="rf-field">
                    <label className="rf-label" htmlFor="rf-to">{form.mode === "outside" ? "Closes" : "To"}</label>
                    <input id="rf-to" type="time" value={form.to} onChange={set("to")} />
                  </div>
                </div>
              )}
              {form.mode === "during" && form.from > form.to && <p className="rf-help">This window crosses midnight, which is fine.</p>}
            </div>

            <div className="rf-field">
              <span className="rf-label">Days</span>
              <div className="rf-presets">
                {PRESETS.map(([l, d]) => (
                  <button key={l} type="button" className={sameDays(form.days, d) ? "on" : ""} onClick={() => setDays(d)}>{l}</button>
                ))}
              </div>
              <div className="rf-days" role="group" aria-label="Days">
                {DAYS.map((d, i) => (
                  <button key={d} type="button" aria-pressed={form.days.includes(i)} className={"rf-chip" + (form.days.includes(i) ? " on" : "")} onClick={() => toggleDay(i)}>{d}</button>
                ))}
              </div>
              <p className="rf-help">No days selected means every day.</p>
            </div>

            <div className="rf-field">
              <label className="rf-label" htmlFor="rf-cool">Wait before the same alert repeats</label>
              <div className="rf-unit">
                <input id="rf-cool" type="number" inputMode="numeric" min="0" value={form.cooldown} onChange={set("cooldown")} />
                <span>seconds</span>
              </div>
            </div>

            <div className="rf-sum" aria-live="polite">
              <b>{preview.title}</b>
              <small>{preview.detail}</small>
            </div>

            {msg && <p className="err" role="alert" style={{ margin: 0 }}>{msg}</p>}
            <div className="rf-actions">
              <button type="button" className="primary" onClick={saveRule}>Save rule</button>
              <button type="button" className="quiet" onClick={() => { setForm(null); setMsg(""); }}>Cancel</button>
            </div>
          </div>
        ) : (
          <>
            <h3>Alert rules</h3>
            <button className="primary" onClick={() => { setMsg(""); setForm(blankRule()); }}>Add rule</button>
            {rules.length === 0 && <p className="muted">No rules yet. A rule says where, what, when and how loud, for example: Back door, someone is there, outside 08:00 to 18:00, critical.</p>}
            <ul className="rf-rules">
              {rules.map(r => {
                const d = describe(r, areas);
                return (
                  <li key={r.id} className="rf-rule" style={{ opacity: r.enabled ? 1 : 0.55 }}>
                    <span className="txt">
                      <i style={{ display: "inline-block", width: 8, height: 8, borderRadius: 4, background: SEV_COLOR[r.severity], marginRight: 6 }} />
                      {d.title}<small>{d.detail}</small>
                    </span>
                    <span className="btns">
                      <button className="quiet" onClick={() => flip(r)}>{r.enabled ? "Pause" : "Resume"}</button>
                      <button className="quiet" onClick={() => { setMsg(""); setForm(fromRule(r)); }}>Edit</button>
                      <button className="quiet" onClick={() => remove(r.id)}>Delete</button>
                    </span>
                  </li>
                );
              })}
            </ul>
            {msg && <p className="muted" role="status">{msg}</p>}
          </>
        )}
      </div>
    </div>
  );
}

/* ---------------------------------------------------------------- Labels */

const AREA_TYPES = ["door", "hallway", "room", "counter", "restricted", "other"];
const rectPts = (x, y, w, h) => [[x, y], [x + w, y], [x + w, y + h], [x, y + h]];
const cap = t => t[0].toUpperCase() + t.slice(1);

// Per-camera tab: draw labelled areas (door, hallway, ...) that the rules and the AI use to understand where people are.
function Labeler({ cid }) {
  const base = `/api/cameras/${cid}`;
  const [areas, setAreas] = useState([]);
  const [tool, setTool] = useState("poly");
  const [draft, setDraft] = useState(null);       // shape being drawn
  const [form, setForm] = useState(null);         // the area being named: { id?, name, type, note, priority, anchor, pts? }
  const [paths, setPaths] = useState([]);
  const [showPaths, setShowPaths] = useState(false);
  const [msg, setMsg] = useState("");
  const svg = useRef();
  const reload = () => api(`${base}/areas`).then(setAreas).catch(() => setMsg("Can't reach the backend."));
  useEffect(() => { reload(); }, [cid]);
  useEffect(() => { if (showPaths) api(`${base}/positions`).then(setPaths).catch(() => {}); }, [showPaths, cid]);

  const pick = t => { setTool(t); setDraft(null); };
  const pt = e => { const r = svg.current.getBoundingClientRect(); return [((e.clientX - r.left) / r.width) * W, ((e.clientY - r.top) / r.height) * H]; };
  const finish = pts => { setDraft(null); setMsg(""); setForm({ name: "", type: "room", note: "", priority: 0, anchor: "feet", pts }); };
  const undo = () => setDraft(d => (d && d.pts.length > 1 ? { ...d, pts: d.pts.slice(0, -1) } : null));
  const down = e => {
    if (form) return;
    const [x, y] = pt(e);
    if (tool === "poly") {
      if (draft?.pts.length > 2 && Math.hypot(x - draft.pts[0][0], y - draft.pts[0][1]) < 0.5) return finish(draft.pts);
      return setDraft({ t: "poly", pts: [...(draft?.pts || []), [x, y]] });
    }
    e.currentTarget.setPointerCapture(e.pointerId);
    setDraft({ t: "rect", x, y, w: 0, h: 0, ox: x, oy: y });
  };
  const move = e => {
    if (!draft || tool === "poly") return;
    const [x, y] = pt(e);
    setDraft(d => ({ ...d, x: Math.min(x, d.ox), y: Math.min(y, d.oy), w: Math.abs(x - d.ox), h: Math.abs(y - d.oy) }));
  };
  const up = () => {
    if (!draft || tool === "poly") return;
    if (draft.w > 0.3 && draft.h > 0.3) finish(rectPts(draft.x, draft.y, draft.w, draft.h));
    else setDraft(null);
  };

  const saveArea = () => {
    if (!form.name.trim()) return setMsg("Give the area a name.");
    const body = { name: form.name.trim(), type: form.type, note: form.note, priority: Number(form.priority) || 0, anchor: form.anchor };
    const req = form.id ? api(`${base}/areas/${form.id}`, "PUT", body)
      : api(`${base}/areas`, "POST", { ...body, polygon: form.pts.map(([x, y]) => [x / W, y / H]) });
    req.then(() => { setForm(null); setMsg(""); reload(); }).catch(() => setMsg("Could not save the area."));
  };
  const remove = id => {
    if (!window.confirm("Delete this area? Alert rules that use it are deleted too.")) return;
    api(`${base}/areas/${id}`, "DELETE").then(reload).catch(() => {});
  };
  const rebuild = () => api(`${base}/areas/rebuild`, "POST", {}).then(r => setMsg(`History rebuilt: ${r.events} area visits.`)).catch(() => setMsg("Rebuild failed."));
  const set = k => e => setForm({ ...form, [k]: e.target.value });

  const hints = { poly: "Tap each corner of the area. Tap the first point to close the shape.", rect: "Press and drag across the area." };
  const drawing = draft?.t === "poly";
  return (
    <div className="cam-layout">
      <Feed zones={areas.map(areaShape)} hot={false}>
        <svg ref={svg} viewBox={`0 0 ${W} ${H}`} className="draw" onPointerDown={down} onPointerMove={move} onPointerUp={up}>
          {showPaths && paths.map((p, i) => <circle key={i} cx={p[0] * W} cy={p[1] * H} r="0.05" style={{ fill: "#ffc800", opacity: 0.6, pointerEvents: "none" }} />)}
          <AreaNames areas={areas} />
          {form?.pts && <Shape s={{ t: "poly", pts: form.pts }} draft />}
          {draft && <Shape s={draft} draft />}
          {drawing && draft.pts.map((q, i) => <circle key={i} cx={q[0]} cy={q[1]} r="0.08" className="dot" />)}
        </svg>
      </Feed>
      <div className="card">
        {form ? (
          <div className="rf">
            <h3 style={{ margin: 0 }}>{form.id ? "Edit area" : "Name this area"}</h3>

            <div className="rf-field">
              <label className="rf-label" htmlFor="la-name">Name</label>
              <input id="la-name" type="text" value={form.name} onChange={set("name")} placeholder="Back door" />
            </div>

            <div className="rf-field">
              <span className="rf-label">Type</span>
              <Seg label="Area type" value={form.type} onChange={v => setForm(f => ({ ...f, type: v }))} options={AREA_TYPES.map(t => [t, cap(t)])} />
            </div>

            <div className="rf-field">
              <label className="rf-label" htmlFor="la-note">Note for the AI</label>
              <input id="la-note" type="text" value={form.note} onChange={set("note")} placeholder="Staff only, locked after 6 pm" />
              <p className="rf-help">Optional. Helps the AI describe what is going on here.</p>
            </div>

            <div className="rf-field">
              <label className="rf-label" htmlFor="la-prio">Priority</label>
              <input id="la-prio" type="number" inputMode="numeric" value={form.priority} onChange={set("priority")} />
              <p className="rf-help">Where areas overlap, the higher number wins in summaries.</p>
            </div>

            <div className="rf-field">
              <span className="rf-label">Position counted at</span>
              <Seg label="Position anchor" value={form.anchor} onChange={v => setForm(f => ({ ...f, anchor: v }))}
                options={[["feet", "Feet (open floor)"], ["center", "Body centre (behind counters)"]]} />
            </div>

            {msg && <p className="err" role="alert" style={{ margin: 0 }}>{msg}</p>}
            <div className="rf-actions">
              <button type="button" className="primary" onClick={saveArea}>Save area</button>
              <button type="button" className="quiet" onClick={() => { setForm(null); setMsg(""); }}>Cancel</button>
            </div>
          </div>
        ) : (
          <div className="rf">
            <h3 style={{ margin: 0 }}>Label areas</h3>

            <div className="rf-field">
              <span className="rf-label">Shape</span>
              <Seg label="Shape" value={tool} onChange={pick} options={[["poly", "Polygon"], ["rect", "Rectangle"]]} />
              <p className="rf-help">{hints[tool]}</p>
            </div>

            {drawing && (
              <div className="rf-inline">
                <button type="button" className="primary" disabled={draft.pts.length < 3} onClick={() => finish(draft.pts)}>Finish shape</button>
                <button type="button" className="quiet" onClick={undo}>Undo point</button>
                <button type="button" className="quiet" onClick={() => setDraft(null)}>Clear</button>
              </div>
            )}

            <label className="rf-toggle"><input type="checkbox" checked={showPaths} onChange={e => setShowPaths(e.target.checked)} />Show where people walked</label>

            <div className="rf-field">
              <span className="rf-label">Areas ({areas.length})</span>
              {areas.length === 0 && <p className="rf-help">No areas yet. Draw one on the video, then give it a name and type.</p>}
              <ul className="rf-rules" style={{ marginTop: 0 }}>
                {areas.map(a => (
                  <li key={a.id} className="rf-rule">
                    <span className="txt">{a.name}<small>{cap(a.type)}, priority {a.priority}</small></span>
                    <span className="btns">
                      <button className="quiet" onClick={() => { setMsg(""); setForm({ id: a.id, name: a.name, type: a.type, note: a.note || "", priority: a.priority, anchor: a.anchor }); }}>Edit</button>
                      <button className="quiet" onClick={() => remove(a.id)}>Delete</button>
                    </span>
                  </li>
                ))}
              </ul>
            </div>

            <div className="rf-inline"><button type="button" className="quiet" onClick={rebuild}>Rebuild history</button></div>
            {msg && <p className="muted" role="status" style={{ margin: 0 }}>{msg}</p>}
          </div>
        )}
      </div>
    </div>
  );
}

/* ---------------------------------------------------------------- People */

const PEOPLE_CSS = `
.ptable-wrap { overflow-x: auto; }
.ptable { width: 100%; border-collapse: collapse; font-size: 14px; }
.ptable th, .ptable td { text-align: left; padding: 8px 10px; border-bottom: 1px solid rgba(128,128,128,.25); white-space: nowrap; }
.ptable tbody tr { cursor: pointer; }
.ptable tbody tr:hover, .ptable tbody tr.sel { background: rgba(128,128,128,.14); }
.ptable tbody tr.gone td { opacity: .65; }
.ptable td.live, .live { color: #22c55e; font-weight: 600; opacity: 1 !important; }
.pphoto { width: 44px; height: 60px; object-fit: cover; border-radius: 6px; display: block; background: rgba(128,128,128,.2); }
.pdetail { display: grid; grid-template-columns: minmax(0, 1.4fr) minmax(0, 1fr); gap: 16px; margin: 0 0 16px; }
.pdetail .scroll { max-height: 260px; overflow: auto; }
@media (max-width: 800px) { .pdetail { grid-template-columns: 1fr; } }
`;

// Per-camera tab: the live video on top, a table of everyone the tracker sees below it. Updates while the video plays.
function People({ cid }) {
  const base = `/api/cameras/${cid}`;
  const [data, setData] = useState({ run: null, people: [] });
  const [areas, setAreas] = useState([]);
  const [sel, setSel] = useState(null);       // selected person id
  const [track, setTrack] = useState([]);
  const [err, setErr] = useState("");
  const [pin, setPin] = useState(false);
  const top = useRef();
  useEffect(() => { api(`${base}/areas`).then(setAreas).catch(() => {}); }, [cid]);
  useEffect(() => {                            // live: refresh every 2 s while the video plays
    const tick = () => {
      api(`${base}/people`).then(d => { setData(d); setErr(""); }).catch(() => setErr("Can't reach the backend."));
      if (sel != null) api(`${base}/people/${sel}/positions`).then(setTrack).catch(() => {});
    };
    tick();
    const t = setInterval(tick, 2000);
    return () => clearInterval(t);
  }, [cid, sel]);
  const choose = id => {
    setTrack([]);
    setSel(id);
    top.current?.scrollIntoView({ behavior: "smooth", block: "start" });   // jump up to the live video
  };
  const clock = ts => (ts || "").slice(11, 19);
  const line = track.map(p => `${p.x * W},${p.y * H}`).join(" ");
  const people = data.people;
  const person = people.find(p => p.person === sel);
  const last = track[track.length - 1];
  return (
    <>
      <style>{PEOPLE_CSS}</style>
      <div ref={top} className="pdetail" style={pin ? { position: "sticky", top: 0, zIndex: 5 } : undefined}>
        <Feed zones={areas.map(areaShape)} hot={false}>
          <svg viewBox={`0 0 ${W} ${H}`} className="shapes">
            <AreaNames areas={areas} />
            {person && track.length > 1 && <polyline points={line} style={{ fill: "none", stroke: "#ffc800", strokeWidth: 0.06 }} />}
            {person && track.length > 0 && <circle cx={track[0].x * W} cy={track[0].y * H} r="0.14" style={{ fill: "#22c55e" }} />}
            {person && track.length > 1 && <circle cx={last.x * W} cy={last.y * H} r="0.16" style={{ fill: "#ef4444" }} />}
          </svg>
        </Feed>
        <div className="card">
          {person ? (
            <>
              <h3>Person #{person.person} <span className={person.in_view ? "live" : "muted"}>{person.in_view ? "In view" : "Left"}</span></h3>
              <div style={{ display: "flex", gap: 12 }}>
                {person.snapshot && <img className="pphoto" style={{ width: 66, height: 90 }} src={person.snapshot} alt={`Person ${person.person}`} />}
                <div>
                  <p style={{ margin: 0 }}>{person.summary}</p>
                  <p className="muted">First seen {clock(person.first_ts)}, on screen {person.seconds} s. Green dot: first seen. Red dot: latest position.</p>
                </div>
              </div>
              <h3>Positions ({track.length})</h3>
              <div className="scroll">
                <table className="ptable">
                  <thead><tr><th>Time</th><th>x, y</th><th>Area</th></tr></thead>
                  <tbody>{track.map((p, i) => <tr key={i}><td>{clock(p.ts)}</td><td>{p.x.toFixed(2)}, {p.y.toFixed(2)}</td><td>{p.area || "-"}</td></tr>)}</tbody>
                </table>
              </div>
            </>
          ) : <p className="muted">Click a person in the table to see their path on the video.</p>}
        </div>
      </div>

      <div className="card">
        <h3>People{people.length ? ` (${people.filter(p => p.in_view).length} in view, ${people.length} total)` : ""}</h3>
        <label><input type="checkbox" checked={pin} onChange={e => setPin(e.target.checked)} /> Keep the video in view while scrolling</label>
        {err && <p className="err" role="alert">{err}</p>}
        {!err && people.length === 0 && <p className="muted">Nobody yet. People appear here live as the camera sees them.</p>}
        {people.length > 0 && (
          <div className="ptable-wrap">
            <table className="ptable">
              <thead><tr><th>Photo</th><th>Person</th><th>Status</th><th>First seen</th><th>On screen</th><th>Summary</th></tr></thead>
              <tbody>
                {people.map(p => (
                  <tr key={p.person} className={(sel === p.person ? "sel " : "") + (p.in_view ? "" : "gone")} onClick={() => choose(p.person)}>
                    <td>{p.snapshot ? <img className="pphoto" src={p.snapshot} alt={`Person ${p.person}`} /> : <span className="pphoto" />}</td>
                    <td>#{p.person}</td>
                    <td className={p.in_view ? "live" : ""}>{p.in_view ? "In view" : "Left"}</td>
                    <td>{clock(p.first_ts)}</td>
                    <td>{p.seconds} s</td>
                    <td style={{ whiteSpace: "normal" }}>{p.summary}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </>
  );
}

/* ---------------------------------------------------------------- Activity */

const SHOW_COUNTS = false;   // false: the chart shows the shape of the crowd, no person numbers. Set true to show them.

// Fills its container: measures its width, so it stays readable on phones.
function Chart({ buckets }) {
  const box = useRef();
  const [Wd, setWd] = useState(640);
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const fit = () => setWd(Math.max(260, Math.round(el.clientWidth || 640)));
    fit();
    const ro = new ResizeObserver(fit);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const Ht = Wd < 420 ? 190 : 230, L = SHOW_COUNTS ? 34 : 8, B = 30, T = 10, R = 8;
  const pw = Wd - L - R, ph = Ht - T - B, n = buckets.length;
  const max = Math.max(1, ...buckets.map(b => Math.max(b.people, b.peak)));
  const step = Math.ceil(max / 4), yMax = Math.ceil(max / step) * step;
  const ticks = Array.from({ length: yMax / step + 1 }, (_, i) => i * step);
  const x = i => L + ((i + 0.5) * pw) / n;
  const y = v => T + ph - (v / yMax) * ph;
  const bw = Math.max(2, (pw / n) * 0.7);
  const every = Math.max(1, Math.ceil(n / Math.max(2, Math.floor(pw / 64))));
  const txt = { fill: "currentColor", opacity: 0.7, fontSize: 11 };
  return (
    <div ref={box} className="chart-box">
      <svg width={Wd} height={Ht} viewBox={`0 0 ${Wd} ${Ht}`} style={{ display: "block" }} role="img" aria-label="Crowd level over time">
        {ticks.map(v => (
          <g key={v}>
            <line x1={L} x2={Wd - R} y1={y(v)} y2={y(v)} stroke="currentColor" opacity="0.15" />
            {SHOW_COUNTS && <text x={L - 6} y={y(v) + 4} textAnchor="end" style={txt}>{v}</text>}
          </g>
        ))}
        {buckets.map((b, i) => (
          <g key={i}>
            <rect x={x(i) - bw / 2} y={y(b.people)} width={bw} height={ph + T - y(b.people)} fill="#ffc800" opacity="0.85">
              <title>{SHOW_COUNTS ? `${b.label}: ${b.people} people, up to ${b.peak} at once` : b.label}</title>
            </rect>
            {i % every === 0 && <text x={x(i)} y={Ht - 10} textAnchor="middle" style={txt}>{b.label}</text>}
          </g>
        ))}
        <polyline points={buckets.map((b, i) => `${x(i)},${y(b.peak)}`).join(" ")} fill="none" stroke="#22c55e" strokeWidth="2" />
      </svg>
    </div>
  );
}

// Per-camera tab: how busy it was, and where people spent their time.
function Activity({ cid }) {
  const d = useJson(`/api/cameras/${cid}/activity`, 3000);
  if (!d) return <div className="card"><p className="muted">Can't reach the backend.</p></div>;
  if (d.empty) return <div className="card"><h3>Activity</h3><p className="muted">No data yet. The chart fills in while the video plays.</p></div>;
  const size = d.bucket_seconds;
  const unit = size < 60 ? `${size} seconds` : size < 3600 ? `${size / 60} minutes` : "1 hour";
  return (
    <>
      <div className="stats">
        {[["People seen", d.people], ["Most at once", d.peak.count], ["Busiest at", d.peak.at], ["Each bar", unit]].map(([l, v]) => (
          <div key={l} className="stat"><span>{l}</span><strong>{v}</strong></div>
        ))}
      </div>
      <div className="card">
        <h3>Crowd over time</h3>
        <Chart buckets={d.buckets} />
        <p className="muted"><span style={{ color: "#ffc800" }}>Bars</span>: how many different people were seen in that period. <span style={{ color: "#22c55e" }}>Line</span>: the busiest moment in that period.</p>
      </div>
      <div className="card">
        <h3>Busiest areas</h3>
        {d.areas.length === 0 && <p className="muted">No labeled areas have been used yet.</p>}
        {d.areas.map(a => (
          <div key={a.name} style={{ display: "grid", gridTemplateColumns: "minmax(70px, 140px) minmax(0, 1fr) 44px", gap: 10, alignItems: "center", margin: "6px 0" }}>
            <span style={{ overflowWrap: "anywhere" }}>{a.name}</span>
            <span style={{ background: "rgba(128,128,128,.2)", borderRadius: 4, height: 8 }}>
              <span style={{ display: "block", height: 8, borderRadius: 4, background: "#ffc800", width: `${Math.round(a.share * 100)}%` }} />
            </span>
            <span>{Math.round(a.share * 100)}%</span>
          </div>
        ))}
        {d.areas.length > 0 && <p className="muted">Share of the time people spent inside labeled areas.</p>}
      </div>
    </>
  );
}

/* ---------------------------------------------------------------- shell */

function Login({ onLogin }) {
  const [email, setEmail] = useState("");
  const [pw, setPw] = useState("");
  const [err, setErr] = useState("");
  const submit = e => {
    e.preventDefault();
    if (!/^\S+@\S+\.\S+$/.test(email)) return setErr("Enter a valid email address.");
    if (pw.length < 4) return setErr("Password must be at least 4 characters.");
    const n = email.split("@")[0];
    onLogin({ email, name: n[0].toUpperCase() + n.slice(1) });
  };
  return (
    <div className="page">
      <div className="login">
        <div className="login-art"><h1>Argus</h1><p>Your virtual security guard. Track every person on camera and get an alert when your rules say something is wrong.</p></div>
        <form onSubmit={submit} noValidate>
          <h2>Sign in</h2>
          <label>Email<input value={email} onChange={e => setEmail(e.target.value)} autoComplete="email" /></label>
          <label>Password<input type="password" value={pw} onChange={e => setPw(e.target.value)} autoComplete="current-password" /></label>
          {err && <p className="err" role="alert">{err}</p>}
          <button className="primary">Sign in</button>
          <p className="muted"></p>
        </form>
      </div>
    </div>
  );
}

// One camera: alerts, labelled areas, live people and activity, as tabs.
function CameraPage({ cam }) {
  const cid = cam.src ?? cam.id;   // demo cameras read Camera 1's data
  const [tab, setTab] = useState("alerts");
  const tabs = [["alerts", "Alerts"], ["labels", "Labels"], ["people", "People"], ["activity", "Activity"]];
  return (
    <>
      <div className="seg" role="tablist" aria-label={`${cam.name} sections`} style={{ marginBottom: 12 }}>
        {tabs.map(([id, label]) => (
          <button key={id} role="tab" aria-selected={tab === id} className={tab === id ? "on" : ""} onClick={() => setTab(id)}>{label}</button>
        ))}
      </div>
      {tab === "alerts" ? <Alerts cid={cid} /> : tab === "labels" ? <Labeler cid={cid} />
        : tab === "people" ? <People cid={cid} /> : <Activity cid={cid} />}
    </>
  );
}

function Dashboard({ user, onLogout }) {
  const { health, setHealth, events, ack } = useBackend();
  const [view, setView] = useState(0);
  const [panel, setPanel] = useState(false);
  const [now, setNow] = useState(Date.now());
  const imgRef = useRef(null);
  const online = !!health, live = !!health?.online;
  const rules = useJson(online ? "/api/cameras/1/rules" : null, 5000);
  const areas = useJson(online ? "/api/cameras/1/areas" : null, 5000);
  useEffect(() => { const t = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(t); }, []);
  const toggleArm = () => api("/api/arm", "PUT", { armed: !health.armed }).then(r => setHealth(h => ({ ...h, armed: r.armed }))).catch(() => {});
  const camOf = n => n.cam || 1;
  const hot = new Set([...(health?.alerts || []), ...events.filter(n => now - n.ts * 1000 < 8000).map(camOf)]);
  const unread = events.filter(n => n.status === "new").length;
  const today = events.filter(n => new Date(n.ts * 1000).toDateString() === new Date().toDateString()).length;
  const ruleCount = (rules || []).filter(r => r.enabled).length;
  const open = n => { ack([n.id]); setView(camOf(n)); setPanel(false); };
  const readAll = () => ack(events.filter(n => n.status === "new").map(n => n.id));
  const test = () => api("/api/dev/fake-event", "POST", { cam: 1 }).catch(() => {});
  const cam = CAMS.find(c => c.id === view);
  const stats = [["Cameras online", live ? CAMS.length : 0], ["Alerts today", today], ["Active rules", ruleCount], ["Unread alerts", unread]];
  const banner = !online ? "Can't reach the backend. Open a terminal in the backend folder and run: python app.py"
    : !live ? "The backend is running but can't open the video. Check the SOURCE path you set before running python app.py." : "";

  return (
    <Src.Provider value={{ img: imgRef, live }}>
      <style>{RULE_CSS}</style>
      <img key={live ? "on" : "off"} ref={imgRef} className="src" alt="" src={live ? "/api/cameras/1/stream" : undefined} />
      <div className="page">
        <div className={"shell" + (panel ? " show-panel" : "")}>
          <aside className="side">
            <div className="logo">Argus</div>
            <nav aria-label="Main">
              <p className="group">Dashboards</p>
              <button className={"nav" + (!view ? " on" : "")} onClick={() => setView(0)}><Icon n="grid" />Overview</button>
              <p className="group">Cameras</p>
              {CAMS.map(c => (
                <button key={c.id} className={"nav" + (view === c.id ? " on" : "")} onClick={() => setView(c.id)}>
                  <Icon n="cam" />{c.name}{hot.has(c.src) && <span className="pulse" aria-label="Alert" />}
                </button>
              ))}
            </nav>
            <div className="spacer" />
            <div className="user"><span>{user.name}</span><button className="quiet" onClick={onLogout} aria-label="Sign out"><Icon n="out" /></button></div>
          </aside>

          <section className="main">
            <header className="top">
              <p className="crumb">{cam ? "Cameras" : "Dashboards"} <span>/</span> <b>{cam ? cam.name : "Overview"}</b></p>
              <span className="muted">{health ? `${health.people} ${health.people === 1 ? "person" : "people"} in view${health.finished ? " (video finished)" : ""}` : ""}</span>
              <label className="switch"><input type="checkbox" checked={!!health?.armed} disabled={!online} onChange={toggleArm} /><i />{health?.armed ? "Armed" : "Disarmed"}</label>
              <button className="quiet bell" onClick={() => setPanel(p => !p)} aria-label="Notifications"><Icon n="bell" />{unread > 0 && <b>{unread}</b>}</button>
            </header>
            <div className="body">
              {banner && <p className="banner" role="alert">{banner}</p>}
              {!cam ? (
                <>
                  <div className="stats">{stats.map(([l, v]) => <div key={l} className="stat"><span>{l}</span><strong>{v}</strong></div>)}</div>
                  <div className="card"><h3>All cameras</h3>
                    <div className="grid">
                      {CAMS.map(c => (
                        <button key={c.id} className="tile" onClick={() => setView(c.id)}>
                          <Feed zones={(areas || []).map(areaShape)} hot={hot.has(c.src)} />
                          <span>{c.name}</span><em>{(areas || []).length} areas, {ruleCount} rules</em>
                        </button>
                      ))}
                    </div>
                  </div>
                </>
              ) : <CameraPage key={cam.id} cam={cam} />}
            </div>
          </section>

          <aside className="notes" aria-label="Notifications">
            <div className="nh"><h3>Notifications</h3><button className="quiet" onClick={readAll}>Mark all read</button></div>
            {events.length === 0 && <p className="muted">No alerts yet. Add a rule in Alerts, then wait for it to trigger.</p>}
            <ul>
              {events.map(n => (
                <li key={n.id}>
                  <button onClick={() => open(n)} className={n.status === "new" ? "unread" : ""}>
                    {n.snapshot ? <img src={n.snapshot} alt="" /> : <span className="ico"><Icon n="bell" /></span>}
                    <span><b>{n.label}</b><small>{n.severity ? `${n.severity}, ` : ""}{ago(n.ts * 1000)}</small></span>
                  </button>
                  {n.frame_snapshot && <a className="quiet" href={n.frame_snapshot} target="_blank" rel="noreferrer">Full frame</a>}
                </li>
              ))}
            </ul>
            <button className="quiet test" onClick={test} disabled={!online}>Send test alert</button>
          </aside>
        </div>
      </div>
    </Src.Provider>
  );
}

export default function App() {
  const [user, setUser] = useState(() => load("argus.user", null));
  if (!user) return <Login onLogin={u => { save("argus.user", u); setUser(u); }} />;
  return <Dashboard user={user} onLogout={() => { localStorage.removeItem("argus.user"); setUser(null); }} />;
}