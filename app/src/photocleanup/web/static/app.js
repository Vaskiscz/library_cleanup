/* Library Cleanup — WebView front-end. Vanilla JS, talks only to 127.0.0.1.
   Flow: Home (idle -> analyze -> category picker) -> Review grid -> Finalize. */

const $ = (sel, root = document) => root.querySelector(sel);
const app = $("#app");

const api = {
  async get(path) {
    const r = await fetch(path);
    if (!r.ok) throw new Error(`${path} -> ${r.status}`);
    return r.json();
  },
  async post(path, body, opts = {}) {
    const r = await fetch(path, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}), ...opts,
    });
    if (!r.ok) throw new Error(`${path} -> ${r.status}`);
    return r.json();
  },
};

const fmtN = (n) => (n || 0).toLocaleString();
const fmtSave = (b) => {           // savings: MB under 1000 MB, else GB
  const mb = (b || 0) / (1024 * 1024);
  return mb < 1000 ? `${mb < 10 ? mb.toFixed(1) : Math.round(mb)} MB` : `${(mb / 1024).toFixed(1)} GB`;
};
const fmtSize = (mb) => (mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB`
  : `${mb < 10 ? (mb || 0).toFixed(1) : Math.round(mb)} MB`);
const icon = (id, cls = "") => `<svg class="${cls}"><use href="#${id}"/></svg>`;

const CATS = [
  { id: "dedup", name: "Duplicate photoshoots", grouped: true, setword: "bursts", noun: "photos",
    desc: "Fifty frames of the same view — the sharpest one is pre-picked." },
  { id: "screenshots", name: "Screenshots", grouped: false, noun: "screenshots",
    desc: "Snapped once and never opened again." },
  { id: "videos", name: "Duplicate videos", grouped: true, setword: "sets", noun: "videos",
    desc: "Ten takes of one moment — the steady one stays." },
  { id: "expired", name: "Expired utility photos", grouped: false, noun: "photos",
    desc: "That parking-spot photo from a garage you left in 2022." },
];
const CAT = Object.fromEntries(CATS.map((c) => [c.id, c]));

const state = {
  view: "home",
  phase: "idle",            // idle | scanning | results
  libStatus: "unknown",     // unknown | connected | error  (drives the status dot)
  version: "",              // app version, shown in the home footer
  errorMsg: "",             // last analyze failure (shown on the home screen)
  errorLog: "",             // path to the diagnostic log for that failure
  lib: null,                // {photos, videos}
  cardSize: 116,            // review preview size (px), slider + cmd+wheel
  summary: null,            // {layer: {groups, items, removable, reclaimable_bytes}}
  selected: new Set(),
  candidates: {},           // layer -> groups
  decisions: {},            // layer -> {uuid: 'keep'|'remove'}
  collapsed: new Set(),
  finalize: null,           // null | 'confirm' | 'working' | 'done'
  done: null,
  months: [],               // global month axis ["YYYY-MM", …] for the time filter
  range: null,              // {lo, hi} indices into months (null = all)
  selUuid: null,            // review: the card shown in the preview panel
  selLayer: null,
  pvCollapsed: false,       // review: preview panel hidden
  pvWidth: 324,             // review: preview panel width (px), user-draggable
};

// (analysis runs as a background job; the UI polls /api/progress)

/* ---- chrome --------------------------------------------------------------- */
function chrome(inner) {
  // The dot/text reflect REAL state: grey until we've actually read the library,
  // green once a scan succeeded, red if access failed.
  let dot = "idle", text = "Library not connected";
  if (state.libStatus === "error") {
    dot = "err"; text = "Library access needed";
  } else if (state.libStatus === "connected") {
    dot = "ok";
    text = state.lib
      ? `Library connected · ${fmtN(state.lib.photos)} photos · ${fmtN(state.lib.videos)} videos`
      : "Library connected";
  }
  const ver = state.version
    ? ` <span style="font-size:var(--pc-text-xs);font-weight:var(--pc-weight-medium);color:var(--pc-text-tertiary)">v${state.version}</span>`
    : "";
  return `
    <div class="chrome">
      <div class="topbar">
        <div class="brand" style="align-items:baseline"><svg viewBox="0 0 1024 1024" style="align-self:center"><use href="#appicon"/></svg> Library Cleanup${ver}</div>
        <div class="status"><span class="dot ${dot}"></span>${text}</div>
      </div>
      ${inner}
    </div>`;
}

function render() {
  if (state.view === "home") renderHome();
  else renderReview();
}

/* ---- home ----------------------------------------------------------------- */
function renderHome() {
  let body = "";
  if (state.phase === "idle") {
    const err = state.libStatus === "error" ? `
        <div class="errbox">
          <div class="errttl">Couldn't read your photo library</div>
          <div class="errmsg">Library Cleanup needs <b>Full Disk Access</b>. Open
            System Settings ▸ Privacy &amp; Security ▸ <b>Full Disk Access</b>, enable
            <b>Library Cleanup</b>, then click Analyze again.</div>
          <div class="errrow">
            <button class="btn-secondary sm" id="openlog">Open log to send the developer</button>
          </div>
        </div>` : "";
    body = `
      <div class="scroll"><div class="home"><div class="hero">
        <svg class="appicon" viewBox="0 0 1024 1024"><use href="#appicon"/></svg>
        <h1>Your photo library, minus the junk drawer</h1>
        <p class="sub">Years of near-identical bursts and one-and-done screenshots are quietly
          eating your storage. One scan finds the keepers, flags the rest, and hands back the gigabytes.</p>
        ${err}
        <div class="checks-lbl">On the hunt for</div>
        <div class="checks">
          <div class="check-card"><span class="ic">${icon("i-stack")}</span><span class="ct">Burst clones</span><span class="cd">Fifty shots of one sunset. The sharpest survives.</span></div>
          <div class="check-card"><span class="ic">${icon("i-video")}</span><span class="ct">Repeat takes</span><span class="cd">Ten tries at the same clip. The steady one wins.</span></div>
          <div class="check-card"><span class="ic">${icon("i-shot")}</span><span class="ct">Screenshot pile</span><span class="cd">Snapped once, never opened again. Buh-bye.</span></div>
        </div>
        <button class="btn btn-primary" id="analyze">Analyze Library</button>
        <div class="past">Takes a minute. No commitment — nothing's deleted until you say so.</div>
      </div></div></div>
      <div class="foot-note">${icon("i-lock")} Runs entirely on your Mac. Your photos never go anywhere.</div>`;
  } else if (state.phase === "scanning") {
    body = `
      <div class="scroll"><div class="home"><div class="scanning">
        <div class="spinner"></div>
        <h2>Analyzing your library…</h2>
        <div class="step" id="step">Starting…</div>
        <div class="progress indet" id="prog"><span id="bar" style="width:100%"></span></div>
        <div class="count" id="count"></div>
        <div style="margin-top:22px"><button class="btn-secondary" id="cancel">Cancel</button></div>
      </div></div></div>`;
  } else {
    body = categoriesBody();
  }
  app.innerHTML = chrome(body);
  bindHome();
}

/* ---- categories (post-scan picker + reclaim banner + time filter) --------- */
function categoriesBody() {
  buildMonthAxis();
  const tot = filteredTotals();
  const filter = state.months.length > 1 ? timeFilterHtml() : "";
  return `<div class="scroll"><div class="home"><div class="results" style="padding-top:0">
      <div class="reclaim">
        <div class="big">Up to <em id="totGb">${fmtSave(tot.bytes)}</em> ready to clear</div>
        <div class="sub">Pick the categories worth a look. Nothing is removed until you confirm.</div>
      </div>
      ${filter}
      <div class="cat-list" style="margin-top:18px">${CATS.map(catCard).join("")}</div>
    </div></div></div>
    ${resultsBar()}`;
}

function timeFilterHtml() {
  const { lo, hi } = state.range, max = state.months.length - 1;
  return `<div class="tfilter">
    <div class="tf-head">
      <span class="tf-title">${icon("i-clock")} Time period</span>
      <span><span class="tf-range" id="tfRange"></span><button class="tf-reset" id="tfReset">Reset</button></span>
    </div>
    <div class="tf-chart" id="tfChart" aria-hidden="true"></div>
    <div class="range-wrap">
      <div class="rtrack"></div><div class="rfill" id="rfill"></div>
      <input type="range" id="rStart" min="0" max="${max}" value="${lo}" aria-label="Start month">
      <input type="range" id="rEnd" min="0" max="${max}" value="${hi}" aria-label="End month">
    </div>
    <div class="ticks" id="ticks"></div>
  </div>`;
}

function catCard(c) {
  const s = state.summary && state.summary[c.id];
  const identified = s && s.items > 0;
  const f = filteredCat(c.id);
  const has = identified && f.items > 0;
  const on = state.selected.has(c.id) && has;
  const sub = c.grouped ? `across ${fmtN(f.groups)} ${c.setword}` : "flagged to remove";
  const right = !identified
    ? `<span class="none">None identified</span>`
    : `<div class="count${has ? "" : " dim"}"><span data-count>${fmtN(f.items)}</span> <span style="font-weight:400;color:var(--pc-text-tertiary)">${c.noun}</span></div>
       <div class="save" data-save>${has ? `Save up to ${fmtSave(f.bytes)}` : "Nothing here"}</div>
       <div class="desc"><span data-groups>${has ? sub : "—"}</span></div>`;
  return `
    <button class="cat ${on ? "on" : ""} ${identified ? "" : "disabled"}" data-cat="${c.id}" ${identified ? "" : "disabled"} style="${identified && !has ? "opacity:.55" : ""}">
      <span class="check">${icon("i-check")}</span>
      <span class="body"><div class="name">${c.name}</div><div class="desc">${c.desc}</div></span>
      <span class="right">${right}</span>
    </button>`;
}

function resultsBar() {
  const tot = filteredTotals(), n = selectedInRange().length;
  return `<div class="bar bottom">
      <button class="btn-secondary" id="rescan">Re-scan</button>
      <div style="flex:1"></div>
      <div style="color:var(--pc-text-tertiary)" id="barInfo">${n} categor${n === 1 ? "y" : "ies"} · save up to ${fmtSave(tot.bytes)}</div>
      <button class="btn btn-primary" id="review" ${n ? "" : "disabled"}>Review ${n} categor${n === 1 ? "y" : "ies"}</button>
    </div>`;
}

/* ---- time-filter helpers -------------------------------------------------- */
const MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
function monthStr(ts) {
  if (!ts) return null;
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}
function monthLabel(m) { const [y, mo] = m.split("-"); return `${MONTH_NAMES[+mo - 1]} ${y}`; }
function nextMonth(m) { let [y, mo] = m.split("-").map(Number); mo++; if (mo > 12) { mo = 1; y++; } return `${y}-${String(mo).padStart(2, "0")}`; }
function groupMonth(g) {
  const ts = g.photos.map((p) => p.timestamp).filter(Boolean);
  return ts.length ? monthStr(Math.min(...ts)) : null;
}
function buildMonthAxis() {
  const ms = new Set();
  for (const c of CATS) { const s = state.summary && state.summary[c.id]; if (s && s.months) s.months.forEach((e) => ms.add(e.m)); }
  if (!ms.size) { state.months = []; state.range = null; return; }
  const sorted = [...ms].sort(), last = sorted[sorted.length - 1], axis = [];
  let cur = sorted[0];
  while (cur <= last) { axis.push(cur); if (cur === last) break; cur = nextMonth(cur); if (axis.length > 1200) break; }
  state.months = axis;
  if (!state.range || state.range.hi > axis.length - 1) state.range = { lo: 0, hi: axis.length - 1 };
}
function filteredCat(layer) {
  const s = state.summary && state.summary[layer];
  if (!s) return { items: 0, bytes: 0, groups: 0 };
  if (!state.months.length || !s.months) return { items: s.items, bytes: s.reclaimable_bytes, groups: s.groups };
  const loM = state.months[state.range.lo], hiM = state.months[state.range.hi];
  let items = 0, bytes = 0, groups = 0;
  for (const e of s.months) if (e.m >= loM && e.m <= hiM) { items += e.items; bytes += e.bytes; groups += e.groups; }
  return { items, bytes, groups };
}
function selectedInRange() { return [...state.selected].filter((id) => filteredCat(id).items > 0); }
function filteredTotals() {
  const ids = selectedInRange();
  let items = 0, bytes = 0;
  for (const id of ids) { const f = filteredCat(id); items += f.items; bytes += f.bytes; }
  return { items, bytes };
}
function rangeIsAll() { return !state.range || (state.range.lo === 0 && state.range.hi === state.months.length - 1); }

function buildHistogram() {
  const chart = $("#tfChart"), ticks = $("#ticks"); if (!chart) return;
  const axis = state.months;
  const byMonth = new Map();                                  // total reclaimable bytes per month
  for (const c of CATS) {
    const s = state.summary[c.id];
    if (s && s.months) for (const e of s.months) byMonth.set(e.m, (byMonth.get(e.m) || 0) + e.bytes);
  }
  const vals = axis.map((m) => byMonth.get(m) || 0);
  const max = Math.max(1, ...vals);
  const bars = document.createDocumentFragment();
  axis.forEach((m, i) => {
    const bar = document.createElement("div"); bar.className = "tf-bar";
    bar.style.height = (8 + vals[i] / max * 92) + "%";
    bar.title = `${monthLabel(m)} · up to ${fmtSave(vals[i])}`;
    bars.appendChild(bar);
  });
  chart.innerHTML = ""; chart.appendChild(bars);
  const tickFrag = document.createDocumentFragment();
  [...new Set(axis.map((m) => m.slice(0, 4)))].forEach((y) => {
    const idx = axis.findIndex((m) => m.startsWith(y));
    const sp = document.createElement("span"); sp.textContent = y;
    sp.style.left = (idx / (axis.length - 1) * 100) + "%"; tickFrag.appendChild(sp);
  });
  ticks.innerHTML = ""; ticks.appendChild(tickFrag);
}
function updateFilter() {
  const axis = state.months; if (!axis.length) { updateResultsBar(); return; }
  const { lo, hi } = state.range, pct = (v) => v / (axis.length - 1) * 100;
  const rfill = $("#rfill"); if (rfill) { rfill.style.left = pct(lo) + "%"; rfill.style.width = (pct(hi) - pct(lo)) + "%"; }
  app.querySelectorAll(".tf-bar").forEach((b, i) => b.classList.toggle("in", i >= lo && i <= hi));
  const all = rangeIsAll();
  const rng = $("#tfRange"); if (rng) { rng.textContent = (all ? "All time · " : "") + monthLabel(axis[lo]) + " – " + monthLabel(axis[hi]); rng.classList.toggle("all", all); }
  const rst = $("#tfReset"); if (rst) rst.classList.toggle("show", !all);
  let selN = 0, selBytes = 0;                                 // tally selected-in-range as we go
  app.querySelectorAll(".cat[data-cat]").forEach((row) => {
    const id = row.dataset.cat, f = filteredCat(id), c = CAT[id];
    const cnt = $("[data-count]", row); if (cnt) { cnt.textContent = fmtN(f.items); cnt.parentElement.classList.toggle("dim", f.items === 0); }
    const sv = $("[data-save]", row); if (sv) sv.textContent = f.items ? `Save up to ${fmtSave(f.bytes)}` : "Nothing here";
    const gp = $("[data-groups]", row); if (gp) gp.textContent = f.items ? (c.grouped ? `across ${fmtN(f.groups)} ${c.setword}` : "flagged to remove") : "—";
    row.style.opacity = f.items ? "" : ".55";
    if (!f.items) { state.selected.delete(id); row.classList.remove("on"); }
    else if (state.selected.has(id)) { row.classList.add("on"); selN++; selBytes += f.bytes; }
  });
  const tg = $("#totGb"); if (tg) tg.textContent = fmtSave(selBytes);
  applyResultsBar(selN, selBytes);
}
function applyResultsBar(n, bytes) {
  const info = $("#barInfo"); if (info) info.textContent = `${n} categor${n === 1 ? "y" : "ies"} · save up to ${fmtSave(bytes)}`;
  const rv = $("#review"); if (rv) { rv.disabled = !n; rv.textContent = `Review ${n} categor${n === 1 ? "y" : "ies"}`; }
}
function updateResultsBar() {
  const tot = filteredTotals();
  applyResultsBar(selectedInRange().length, tot.bytes);
}

function bindHome() {
  const a = $("#analyze"); if (a) a.onclick = startAnalyze;
  const ol = $("#openlog"); if (ol) ol.onclick = () => api.post("/api/open-log").catch(() => {});
  const c = $("#cancel"); if (c) c.onclick = () => { state.cancelled = true; state.phase = "idle"; render(); };
  const rs = $("#rescan"); if (rs) rs.onclick = () => { state.phase = "idle"; render(); };
  const rv = $("#review"); if (rv) rv.onclick = enterReview;
  app.querySelectorAll(".cat[data-cat]").forEach((btn) => {
    btn.onclick = () => {
      const id = btn.dataset.cat;
      if (filteredCat(id).items === 0) return;          // nothing here in this range
      state.selected.has(id) ? state.selected.delete(id) : state.selected.add(id);
      btn.classList.toggle("on", state.selected.has(id));
      updateResultsBar();
    };
  });
  if (state.phase === "results" && state.months.length > 1) {
    buildHistogram();
    const rStart = $("#rStart"), rEnd = $("#rEnd");
    if (rStart) rStart.oninput = () => { if (+rStart.value > +rEnd.value) rStart.value = rEnd.value; state.range.lo = +rStart.value; updateFilter(); };
    if (rEnd) rEnd.oninput = () => { if (+rEnd.value < +rStart.value) rEnd.value = rStart.value; state.range.hi = +rEnd.value; updateFilter(); };
    const rst = $("#tfReset"); if (rst) rst.onclick = () => {
      state.range = { lo: 0, hi: state.months.length - 1 };
      rStart.value = 0; rEnd.value = state.months.length - 1; updateFilter();
    };
    updateFilter();
  }
}

async function startAnalyze() {
  state.phase = "scanning";
  state.cancelled = false;
  render();
  try {
    await api.post("/api/analyze", { layers: CATS.map((c) => c.id) });
  } catch (e) {
    state.phase = "idle"; render(); alert("Couldn't start analysis: " + e.message); return;
  }
  pollProgress();
}

async function pollProgress() {
  if (state.cancelled || state.phase !== "scanning") return;
  let p;
  try { p = await api.get("/api/progress"); }
  catch { return void setTimeout(pollProgress, 600); }
  updateScanning(p);
  if (p.status === "done") {
    state.libStatus = "connected";   // the library was read successfully
    state.summary = p.summary;
    state.selected = new Set(CATS.filter((c) => p.summary[c.id]?.items > 0).map((c) => c.id));
    api.get("/api/library-stats").then((s) => { state.lib = s; render(); }).catch(() => {});
    state.phase = "results"; render();
  } else if (p.status === "error") {
    state.libStatus = "error";
    state.errorMsg = p.error || "Something went wrong.";
    state.errorLog = p.log || "";
    state.phase = "idle"; render();
  } else {
    setTimeout(pollProgress, 400);
  }
}

function updateScanning(p) {
  const step = $("#step"), bar = $("#bar"), count = $("#count"), prog = $("#prog");
  if (!step) return;
  step.textContent = p.message || "Working…";
  if (p.total) {
    prog.classList.remove("indet");
    // bar follows overall progress (incl. post-passes); the count is items scanned
    const frac = (p.frac != null) ? p.frac : (p.done / p.total);
    bar.style.width = Math.round(frac * 100) + "%";
    count.textContent = `${fmtN(p.done)} / ${fmtN(p.total)}`;
  } else {
    prog.classList.add("indet");
    bar.style.width = "100%";
    count.textContent = "";
  }
}

/* ---- review --------------------------------------------------------------- */
async function enterReview() {
  state.view = "review";
  state.candidates = {};
  state.decisions = {};
  pvCache.clear();                 // drop cached preview images from the previous review
  app.innerHTML = chrome(`<div class="scroll"><div class="review"><div class="scanning">
      <div class="spinner"></div><h2>Loading review…</h2></div></div></div>`);
  const layers = CATS.map((c) => c.id).filter((id) => state.selected.has(id));
  const all = rangeIsAll();
  const loM = all ? null : state.months[state.range.lo];
  const hiM = all ? null : state.months[state.range.hi];
  for (const layer of layers) {
    const res = await api.get(`/api/candidates?layer=${layer}`);
    let groups = res.groups;
    if (!all) {                                  // narrow to the picked time period, client-side
      if (CAT[layer].grouped) {
        groups = groups.filter((g) => { const m = groupMonth(g); return m && m >= loM && m <= hiM; });
      } else {
        groups = groups
          .map((g) => ({ ...g, photos: g.photos.filter((p) => { const m = monthStr(p.timestamp); return m && m >= loM && m <= hiM; }) }))
          .filter((g) => g.photos.length);
      }
    }
    state.candidates[layer] = groups;
    const d = {};
    groups.forEach((g) => g.photos.forEach((p) => {
      d[p.uuid] = p.decided ? (p.decided === "keep" ? "keep" : "remove")
        : (p.suggested_keep ? "keep" : "remove");
    }));
    state.decisions[layer] = d;
  }
  state.selUuid = null; state.selLayer = null; state.pvCollapsed = false;
  renderReview();
}

function counts() {
  let keep = 0, rem = 0, bytes = 0, items = 0;
  for (const layer of Object.keys(state.decisions)) {
    const groups = state.candidates[layer] || [];
    const byId = {};
    groups.forEach((g) => g.photos.forEach((p) => (byId[p.uuid] = p)));
    for (const [uuid, v] of Object.entries(state.decisions[layer])) {
      items++;
      if (v === "keep") keep++;
      else { rem++; bytes += byId[uuid]?.bytes || 0; }
    }
  }
  return { keep, rem, bytes, items };
}

function renderReview() {
  const layers = CATS.map((c) => c.id).filter((id) => state.selected.has(id));
  const sections = layers.map(sectionHtml).join("");
  const c = counts();
  const pct = c.items ? Math.round(((c.keep + c.rem) / c.items) * 100) : 0;
  const body = `
    <div class="bar top">
      <span>${fmtN(c.items)} items in ${layers.length} categor${layers.length === 1 ? "y" : "ies"}</span>
      <span class="mini-prog"><span style="width:${pct}%"></span></span>
      <span><span class="keep-n">Keeping ${fmtN(c.keep)}</span> · <span class="rem-n">Removing ${fmtN(c.rem)}</span></span>
      <span class="bulk">
        <button class="btn-secondary sm" id="keepAll" title="Keep every suggestion in the review">Keep all</button>
        <button class="btn-secondary sm" id="removeAll" title="Remove every suggestion in the review">Remove all</button>
      </span>
      <span class="sizer" title="Preview size (⌘ + scroll)">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><rect x="3" y="3" width="8" height="8" rx="1.5"/><rect x="13" y="3" width="8" height="8" rx="1.5"/><rect x="3" y="13" width="8" height="8" rx="1.5"/><rect x="13" y="13" width="8" height="8" rx="1.5"/></svg>
        <input type="range" min="84" max="240" step="2" value="${state.cardSize}" id="cardsize">
      </span>
    </div>
    <div class="rv-main ${state.pvCollapsed ? "collapsed" : ""}" id="rvMain">
      <div class="scroll"><div class="review">${sections}</div></div>
      <aside class="preview" id="preview">
        <div class="pv-head"><span class="pv-title">Preview</span>
          <button class="pv-collapse" id="pvCollapse" title="Hide preview" aria-label="Hide preview">›</button></div>
        <div class="pv-empty" id="pvEmpty">Click any photo to preview it full-size here.</div>
        <div class="pv-content" id="pvContent" hidden>
          <div class="pv-img" id="pvImg"></div>
          <div class="pv-name" id="pvName"></div>
          <div class="pv-meta" id="pvMeta"></div>
          <button class="btn pv-toggle" id="pvToggle"></button>
        </div>
      </aside>
      <div class="pv-resize" id="pvResize" title="Drag to resize · double-click to reset" role="separator" aria-orientation="vertical"></div>
      <button class="pv-reopen" id="pvReopen" title="Show preview" aria-label="Show preview">‹</button>
    </div>
    <div class="bar bottom">
      <button class="btn-secondary" id="back">‹ Back</button>
      <div style="flex:1"></div>
      <div style="color:var(--pc-text-tertiary)">
        <span class="keep-n">Keeping ${fmtN(c.keep)}</span> ·
        <span class="rem-n">Removing ${fmtN(c.rem)}</span> · Frees ${fmtSave(c.bytes)}</div>
      <button class="btn btn-primary" id="finalize" ${c.rem ? "" : "disabled"}>Review &amp; Finalize</button>
    </div>`;
  app.innerHTML = chrome(body) + (state.finalize ? modalHtml() : "");
  bindReview();
}

function sectionHtml(layer) {
  const c = CAT[layer];
  const groups = state.candidates[layer] || [];
  const d = state.decisions[layer] || {};
  let keep = 0, rem = 0, items = 0;
  groups.forEach((g) => g.photos.forEach((p) => { items++; d[p.uuid] === "keep" ? keep++ : rem++; }));
  const summary = c.grouped
    ? `${fmtN(groups.length)} ${c.setword} · keeping ${keep} · removing ${rem}`
    : `${fmtN(items)} flagged · keeping ${keep} · removing ${rem}`;
  const blocks = c.grouped
    ? groups.map((g) => groupHtml(layer, g)).join("")
    : (groups[0] ? flatHtml(layer, groups[0]) : "");
  return `<div class="section" data-layer="${layer}"><h3>${c.name}</h3><div class="summary">${summary}</div>${blocks}</div>`;
}

function groupHtml(layer, g) {
  const noun = layer === "videos" ? "clips" : "shots";
  const d = state.decisions[layer];
  const keep = g.photos.filter((p) => d[p.uuid] === "keep").length;
  const rem = g.photos.length - keep;
  const collapsed = state.collapsed.has(g.group_key);
  return `<div class="group ${collapsed ? "collapsed" : ""}" data-group="${g.group_key}"
              data-layer="${layer}" data-noun="${noun}" data-size="${g.size}">
    <div class="ghead">
      <span class="gtitle">${g.title}</span>
      <span class="gmeta">· ${g.size} ${noun} · keep ${keep} · remove ${rem}</span>
      <span class="spacer"></span>
      <button class="btn-secondary sm" data-all="keep" data-layer="${layer}" data-g="${g.group_key}">Keep all</button>
      <button class="btn-secondary sm" data-all="remove" data-layer="${layer}" data-g="${g.group_key}">Remove all</button>
      <button class="chev" data-collapse="${g.group_key}">${collapsed ? "▸" : "▾"}</button>
    </div>
    <div class="gbody">${g.photos.map((p) => cardHtml(layer, p)).join("")}</div>
  </div>`;
}

function flatHtml(layer, g) {
  return `<div class="group"><div class="ghead">
      <span class="gtitle">All flagged to remove</span><span class="gmeta">· tap any to keep</span>
      <span class="spacer"></span>
      <button class="btn-secondary sm" data-all="keep" data-layer="${layer}" data-g="${g.group_key}">Keep all</button>
      <button class="btn-secondary sm" data-all="remove" data-layer="${layer}" data-g="${g.group_key}">Remove all</button>
    </div><div class="gbody">${g.photos.map((p) => cardHtml(layer, p, true)).join("")}</div></div>`;
}

function cardHtml(layer, p, shot = false) {
  const v = state.decisions[layer][p.uuid];
  const badge = `<span class="badge">${icon(v === "keep" ? "i-check" : "i-x")}</span>`;
  const fav = p.favorite ? `<svg class="fav"><use href="#i-heart"/></svg>` : "";
  let overlay = "";
  if (p.is_video) {
    const dur = p.duration ? `${Math.floor(p.duration / 60)}:${String(Math.round(p.duration % 60)).padStart(2, "0")}` : "";
    overlay = `<div class="vplay">${icon("i-play")}</div>${dur ? `<span class="vdur">${dur}</span>` : ""}`;
  }
  return `<div class="card ${shot ? "shot" : ""} ${v === "keep" ? "keep" : "remove"}" data-uuid="${p.uuid}" data-layer="${layer}">
    <div class="frame" tabindex="0" role="button" aria-pressed="${v === "keep"}">
      <img src="${p.thumb}" loading="lazy" decoding="async" width="240" height="240" alt="">
      ${fav}${overlay}${badge}
    </div>
    <div class="fn">${escapeHtml(p.filename)} · ${fmtSize(p.size_mb)}</div></div>`;
}

function escapeHtml(s) {
  return (s || "").replace(/[&<>"]/g, (m) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[m]));
}

function bindReview() {
  $("#back").onclick = () => { state.view = "home"; state.phase = "results"; render(); };
  const fin = $("#finalize"); if (fin) fin.onclick = openFinalize;
  const cs = $("#cardsize"); if (cs) cs.oninput = (e) => setCardSize(+e.target.value);

  // whole-review keep/remove all (every card across every group, incl. collapsed)
  const setAll = (v) => {
    app.querySelectorAll(".card[data-uuid]").forEach((card) => setCardDecision(card, v));
    if (state.selUuid) fillPreview();   // refresh the preview's toggle label
    refreshCounts();
  };
  const ka = $("#keepAll"); if (ka) ka.onclick = () => setAll("keep");
  const ra = $("#removeAll"); if (ra) ra.onclick = () => setAll("remove");

  // One delegated handler for the whole grid — cheaper than binding every card,
  // and (unlike a per-frame key handler) it can't double-fire with the global
  // keydown once a clicked card grabs focus. Click the image = preview; click the
  // corner badge = keep/remove; ghead buttons = keep/remove-all / collapse.
  const reviewEl = $(".review");
  if (reviewEl) reviewEl.onclick = (e) => {
    const badge = e.target.closest(".badge");
    if (badge) {
      const card = badge.closest(".card[data-uuid]");
      if (card) { flip(card); if (card.dataset.uuid === state.selUuid) fillPreview(); }
      return;
    }
    const all = e.target.closest("[data-all]");
    if (all) {
      const gEl = all.closest(".group");
      gEl.querySelectorAll(".card[data-uuid]").forEach((card) => setCardDecision(card, all.dataset.all));
      if (state.selUuid && gEl.querySelector(`.card[data-uuid="${CSS.escape(state.selUuid)}"]`)) fillPreview();
      refreshCounts();
      return;
    }
    const chev = e.target.closest("[data-collapse]");
    if (chev) {
      const gEl = chev.closest(".group");
      const collapsed = gEl.classList.toggle("collapsed");
      collapsed ? state.collapsed.add(chev.dataset.collapse) : state.collapsed.delete(chev.dataset.collapse);
      chev.textContent = collapsed ? "▸" : "▾";
      return;
    }
    const frame = e.target.closest(".frame");
    if (frame) { const card = frame.closest(".card[data-uuid]"); if (card) selectCard(card); }
  };

  // preview panel: hide / reopen / toggle the previewed card's decision
  const pvC = $("#pvCollapse"); if (pvC) pvC.onclick = () => { state.pvCollapsed = true; $("#rvMain").classList.add("collapsed"); };
  const pvR = $("#pvReopen"); if (pvR) pvR.onclick = () => { state.pvCollapsed = false; $("#rvMain").classList.remove("collapsed"); };
  const pvT = $("#pvToggle"); if (pvT) pvT.onclick = toggleSelected;

  // drag the divider to resize the grid / preview split (double-click resets)
  const rz = $("#pvResize");
  if (rz) {
    rz.onpointerdown = (e) => {
      e.preventDefault();
      const rm = $("#rvMain"); rm.classList.add("resizing");
      try { rz.setPointerCapture(e.pointerId); } catch {}
      const startX = e.clientX, startW = state.pvWidth, maxW = rm.clientWidth - 320;
      const onMove = (ev) => setPreviewWidth(Math.min(maxW, startW + (startX - ev.clientX)));  // drag left = wider
      const onUp = () => {
        rm.classList.remove("resizing");
        rz.removeEventListener("pointermove", onMove);
        rz.removeEventListener("pointerup", onUp);
        try { rz.releasePointerCapture(e.pointerId); } catch {}
      };
      rz.addEventListener("pointermove", onMove);
      rz.addEventListener("pointerup", onUp);
    };
    rz.ondblclick = () => setPreviewWidth(PV_DEFAULT);
  }

  // initialise / restore the previewed card so arrows + Space work the moment you arrive
  if (!state.finalize) {
    let card = selectedCardEl() || app.querySelector(".group:not(.collapsed) .card[data-uuid]");
    if (card) selectCard(card, { scroll: false });
  }

  // finalize modal buttons
  if (state.finalize === "confirm") {
    $("#m-cancel").onclick = () => { state.finalize = null; renderReview(); };
    $("#m-go").onclick = doFinalize;
  } else if (state.finalize === "done") {
    $("#m-new").onclick = () => {
      Object.assign(state, { view: "home", phase: "idle", finalize: null, done: null,
        candidates: {}, decisions: {}, selected: new Set(), summary: null });
      render();
    };
  }
}

function setCardDecision(card, v) {
  const { uuid, layer } = card.dataset;
  state.decisions[layer][uuid] = v;
  card.classList.toggle("keep", v === "keep");
  card.classList.toggle("remove", v !== "keep");
  $(".badge", card).innerHTML = icon(v === "keep" ? "i-check" : "i-x");
  $(".frame", card).setAttribute("aria-pressed", v === "keep");
}

function flip(card) {
  const { uuid, layer } = card.dataset;
  setCardDecision(card, state.decisions[layer][uuid] === "keep" ? "remove" : "keep");
  refreshCounts();
}

// Update all count text (section summaries, group headers, bars) in place —
// no DOM rebuild, so thumbnails never reload.
function refreshCounts() {
  for (const layer of CATS.map((c) => c.id).filter((id) => state.selected.has(id))) {
    const c = CAT[layer], groups = state.candidates[layer] || [], d = state.decisions[layer] || {};
    let keep = 0, rem = 0, items = 0;
    groups.forEach((g) => g.photos.forEach((p) => { items++; d[p.uuid] === "keep" ? keep++ : rem++; }));
    const sum = document.querySelector(`.section[data-layer="${layer}"] .summary`);
    if (sum) sum.textContent = c.grouped
      ? `${fmtN(groups.length)} ${c.setword} · keeping ${keep} · removing ${rem}`
      : `${fmtN(items)} flagged · keeping ${keep} · removing ${rem}`;
  }
  app.querySelectorAll(".group[data-group]").forEach((gEl) => {
    const d = state.decisions[gEl.dataset.layer] || {};
    let keep = 0, total = 0;
    gEl.querySelectorAll(".card[data-uuid]").forEach((card) => {
      total++; if (d[card.dataset.uuid] === "keep") keep++;
    });
    const meta = $(".gmeta", gEl);
    if (meta) meta.textContent = `· ${gEl.dataset.size} ${gEl.dataset.noun} · keep ${keep} · remove ${total - keep}`;
  });
  updateBars();
}

/* ---- review preview panel + keyboard selection --------------------------- */
function photoOf(layer, uuid) {
  for (const g of (state.candidates[layer] || [])) {
    const p = g.photos.find((x) => x.uuid === uuid);
    if (p) return p;
  }
  return null;
}
function selectedCardEl() {
  return state.selUuid ? app.querySelector(`.card[data-uuid="${CSS.escape(state.selUuid)}"]`) : null;
}
function selectCard(card, opts = {}) {
  app.querySelectorAll(".card.selected").forEach((c) => c.classList.remove("selected"));
  card.classList.add("selected");
  state.selUuid = card.dataset.uuid;
  state.selLayer = card.dataset.layer;
  state.pvCollapsed = false;
  const rm = $("#rvMain"); if (rm) rm.classList.remove("collapsed");
  const f = $(".frame", card); if (f) f.focus({ preventScroll: true });   // DOM focus follows the selection
  fillPreview();
  if (opts.scroll !== false) card.scrollIntoView({ block: "nearest" });
}
// Full-res preview elements kept in browser memory and reused, so navigating shows
// the sharp image instantly (no blurry-thumb flash) and never re-fetches. We hold
// the <img> elements (encoded ~1 MB each); the browser manages the decoded bitmaps.
// ±2 in each direction are pre-loaded so arrow-scrolling lands on a ready image.
const PREVIEW_PX = 2048;          // ≥ 2× any panel width → crisp on Retina, fine for pixel-peeping
const PV_CACHE_MAX = 7;           // current + ~2 each side + a little slack
const pvCache = new Map();        // uuid -> HTMLImageElement (LRU)
function pvGet(uuid) {
  let img = pvCache.get(uuid);
  if (img) { pvCache.delete(uuid); pvCache.set(uuid, img); return img; }   // LRU bump
  img = new Image();
  img.decoding = "async"; img.alt = "";
  img.src = `/api/thumb/${uuid}?px=${PREVIEW_PX}`;
  pvCache.set(uuid, img);
  while (pvCache.size > PV_CACHE_MAX) pvCache.delete(pvCache.keys().next().value);
  return img;
}
let pvWarmTimer = null;
function fillPreview() {
  const layer = state.selLayer, uuid = state.selUuid, p = photoOf(layer, uuid);
  const pvImg = $("#pvImg"); if (!pvImg || !p) return;
  const keep = state.decisions[layer][uuid] === "keep";
  // metadata + toggle update instantly (also on a keep/remove toggle of the same card)
  $("#pvName").textContent = p.filename;
  const dims = (p.width && p.height) ? `${p.width} × ${p.height}` : "";
  const dur = p.is_video && p.duration ? `${Math.floor(p.duration / 60)}:${String(Math.round(p.duration % 60)).padStart(2, "0")}` : "";
  $("#pvMeta").textContent = [dims, dur, fmtSize(p.size_mb)].filter(Boolean).join(" · ");
  const pvT = $("#pvToggle");
  pvT.textContent = keep ? "Mark for removal" : "Keep this one";
  pvT.className = "btn pv-toggle " + (keep ? "btn-danger" : "btn-primary");
  $("#pvEmpty").hidden = true; $("#pvContent").hidden = false;

  // Swap the image only when the previewed photo changes — never on a keep/remove
  // toggle. Reuse the cached full-res element (instant + sharp if pre-loaded as a
  // neighbour); the grid thumb is a soft placeholder for the rare cold case.
  if (pvImg.dataset.uuid === uuid) return;
  pvImg.dataset.uuid = uuid;
  pvImg.style.backgroundImage = `url("${p.thumb}")`;
  const img = pvGet(uuid);
  pvImg.innerHTML = "";
  pvImg.appendChild(img);
  if (img.complete && img.naturalWidth) img.classList.add("ready");        // already loaded → instant
  else { img.classList.remove("ready"); img.addEventListener("load", () => { if (pvImg.dataset.uuid === uuid) img.classList.add("ready"); }, { once: true }); }
  if (p.is_video) pvImg.insertAdjacentHTML("beforeend", `<div class="vplay">${icon("i-play")}</div>`);

  // Pre-load ±2 in each direction (full-res) once settled, so the next moves are seamless.
  clearTimeout(pvWarmTimer);
  pvWarmTimer = setTimeout(() => { if (state.selUuid === uuid) warmNeighbors(2); }, 120);
}
function warmNeighbors(span) {
  const cards = [...app.querySelectorAll(".group:not(.collapsed) .card[data-uuid]")];
  const i = cards.findIndex((c) => c.dataset.uuid === state.selUuid);
  if (i < 0) return;
  for (let d = 1; d <= span; d++) for (const c of [cards[i - d], cards[i + d]]) {
    if (c) pvGet(c.dataset.uuid);    // loads full-res into the element cache + server RAM cache
  }
}
function moveSelection(key) {
  const cards = [...app.querySelectorAll(".group:not(.collapsed) .card[data-uuid]")];
  if (!cards.length) return;
  const i = cards.findIndex((c) => c.dataset.uuid === state.selUuid);
  if (i < 0) { selectCard(cards[0]); return; }
  const next = cards[key === "ArrowRight" ? i + 1 : i - 1];
  if (next) selectCard(next);
}
function toggleSelected() {
  const card = selectedCardEl();
  if (card) { flip(card); fillPreview(); }
}

function updateBars() {
  // light-touch refresh of the counters without rebuilding the grid
  const c = counts();
  const pct = c.items ? Math.round(((c.keep + c.rem) / c.items) * 100) : 0;
  app.querySelectorAll(".keep-n").forEach((e) => e.textContent = `Keeping ${fmtN(c.keep)}`);
  app.querySelectorAll(".rem-n").forEach((e) => e.textContent = `Removing ${fmtN(c.rem)}`);
  const mp = $(".mini-prog > span"); if (mp) mp.style.width = pct + "%";
  const fin = $("#finalize"); if (fin) fin.disabled = !c.rem;
}

/* ---- finalize ------------------------------------------------------------- */
function openFinalize() { state.finalize = "confirm"; renderReview(); }

function modalHtml() {
  const c = counts();
  if (state.finalize === "confirm") {
    return `<div class="backdrop"><div class="modal">
      <h3>Review &amp; Finalize</h3>
      <p class="head-n">Keeping ${fmtN(c.keep)} · removing ${fmtN(c.rem)} items · frees ${fmtSave(c.bytes)}</p>
      <div class="rows">
        <div class="row">${tick()}<span>macOS will ask you to confirm before anything is removed.</span></div>
        <div class="row">${tick()}<span>Removed items go to Recently Deleted — recoverable for 30 days.</span></div>
        <div class="row">${tick()}<span>Kept items are marked reviewed and won't be shown again. Nothing leaves your Mac.</span></div>
      </div>
      <div class="actions">
        <button class="btn-secondary" id="m-cancel">Go back</button>
        <button class="btn btn-danger" id="m-go">Remove ${fmtN(c.rem)}</button>
      </div></div></div>`;
  }
  if (state.finalize === "working") {
    return `<div class="backdrop"><div class="modal center"><h3>Removing…</h3>
      <div class="working"><div class="spinner sm"></div>
      <div style="color:var(--pc-text-secondary)">Asking Photos to remove the selected items…</div></div></div></div>`;
  }
  return doneHtml();
}

function tick() { return `<svg class="tick"><use href="#i-check"/></svg>`; }

async function doFinalize() {
  const snapshot = counts();
  state.finalize = "working";
  renderReview();
  try {
    const layers = Object.keys(state.decisions);
    for (const layer of layers) {
      const decisions = Object.entries(state.decisions[layer]).map(([uuid, v]) => {
        const p = (state.candidates[layer] || []).flatMap((g) => g.photos).find((x) => x.uuid === uuid);
        return { uuid, verdict: v === "keep" ? "keep" : "discard",
          group_key: p ? findGroupKey(layer, uuid) : null, suggested: p ? p.suggested_keep : false };
      });
      await api.post("/api/decisions", { layer, decisions });
    }
    const fin = await api.post("/api/finalize", { layers });
    const del = await api.post("/api/delete", { uuids: fin.to_delete });
    state.done = { status: del.status, deleted: del.deleted || 0,
                   kept: snapshot.keep, bytes: snapshot.bytes };
    state.finalize = "done";
    renderReview();
  } catch (e) {
    state.finalize = null; renderReview(); alert("Finalize failed: " + e.message);
  }
}

function doneHtml() {
  const d = state.done || {};
  if (d.status === "unauthorized") {
    return `<div class="backdrop"><div class="modal center">
      <div class="done-disc" style="background:var(--pc-warn)">${icon("i-lock")}</div>
      <h3>Photos access needed</h3>
      <p class="head-n">To remove items, allow Library Cleanup in System Settings ▸ Privacy &amp;
        Security ▸ Photos, then run the review again.</p>
      <p class="head-n" style="font-size:12px">Your keepers are already marked reviewed.</p>
      <div class="actions" style="justify-content:center"><button class="btn btn-primary" id="m-new">Back to start</button></div>
    </div></div>`;
  }
  if (d.status && d.status !== "ok") {
    return `<div class="backdrop"><div class="modal center">
      <div class="done-disc" style="background:var(--pc-warn)">${icon("i-x")}</div>
      <h3>Nothing was removed</h3>
      <p class="head-n">${d.status === "error" ? "Removal was cancelled." : "No matching items were found."}
        Your keepers are marked reviewed.</p>
      <div class="actions" style="justify-content:center"><button class="btn btn-primary" id="m-new">Start a new review</button></div>
    </div></div>`;
  }
  return `<div class="backdrop"><div class="modal center">
    <div class="done-disc">${icon("i-check")}</div>
    <h3>All done</h3>
    <p class="head-n">Removed ${fmtN(d.deleted)} · kept ${fmtN(d.kept)} · freed up to ${fmtSave(d.bytes)}.</p>
    <p class="head-n" style="font-size:12px">Removed items stay in Recently Deleted for 30 days.</p>
    <div class="actions" style="justify-content:center"><button class="btn btn-primary" id="m-new">Start a new review</button></div>
  </div></div>`;
}

function findGroupKey(layer, uuid) {
  const g = (state.candidates[layer] || []).find((x) => x.photos.some((p) => p.uuid === uuid));
  return g ? g.group_key : null;
}

/* ---- preview size (slider + ⌘-scroll) ------------------------------------- */
function setCardSize(px) {
  px = Math.max(84, Math.min(240, Math.round(px)));
  state.cardSize = px;
  document.documentElement.style.setProperty("--card", px + "px");
  const cs = $("#cardsize");
  if (cs && +cs.value !== px) cs.value = px;
}

const PV_MIN = 300, PV_DEFAULT = 324;     // preview panel: min keeps content uncramped
function setPreviewWidth(px) {
  state.pvWidth = Math.max(PV_MIN, Math.round(px));
  document.documentElement.style.setProperty("--pv", state.pvWidth + "px");
}

/* ---- boot ----------------------------------------------------------------- */
// Nothing here touches the photo library — the first library access (and any
// Photos permission prompt) happens only when the user clicks "Analyze".
setCardSize(state.cardSize);
setPreviewWidth(state.pvWidth);
// health is library-free (reads only the app's own SQLite) — safe at boot;
// used to show the version in the footer.
api.get("/api/health").then((h) => { state.version = h.version || ""; render(); }).catch(() => {});
document.addEventListener("wheel", (e) => {
  if (!e.metaKey || state.view !== "review") return;
  e.preventDefault();                          // don't let the page zoom
  setCardSize(state.cardSize + (e.deltaY < 0 ? 10 : -10));
}, { passive: false });
// Review keyboard: ← / → move the previewed photo, Space marks/unmarks it.
document.addEventListener("keydown", (e) => {
  if (state.view !== "review" || state.finalize) return;
  if (e.target && e.target.tagName === "INPUT") return;   // let the size slider use arrows
  if (e.key === "ArrowLeft" || e.key === "ArrowRight") { e.preventDefault(); moveSelection(e.key); }
  else if (e.key === " " || e.key === "Enter") { e.preventDefault(); toggleSelected(); }   // the current selection
});
render();
