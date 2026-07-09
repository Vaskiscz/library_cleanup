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

// Open the Ko-fi support page in the default browser (server runs `open`; nothing
// external loads inside the WebView, so the CSP stays strict).
function donate() { api.post("/api/donate").catch(() => {}); }

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
// pseudo-layer for the manual "review everything" feed (not in the picker)
CAT.all = { id: "all", name: "All photos & videos", grouped: false, noun: "items", setword: "items" };
// which layers the review screen shows: the manual feed, or the picked categories
const reviewLayers = () => state.manual ? ["all"] : CATS.map((c) => c.id).filter((id) => state.selected.has(id));

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
  pvUserSized: false,       // true once the user drags the divider (stops auto-sizing)
  manual: false,            // review: manual "all photos & videos" feed (vs. curated)
  allTotal: 0,              // manual feed: total items server-side (pages are fetched lazily)
  allLoading: false,        // manual feed: a page fetch is in flight
  allDefault: "keep",       // manual feed: verdict applied to items that arrive AFTER a bulk keep/remove-all
  update: null,             // {current, latest, notes, ...} when a newer release exists
  updateStatus: "prompt",   // prompt | working | relaunching | error
  updateJob: null,          // {status, frac, message} while downloading/installing
  updateErr: "",
};

// (analysis runs as a background job; the UI polls /api/progress)

/* ---- review persistence (survives quit/crash/reload) ----------------------- */
// Decisions are mirrored to localStorage on every change; the home screen offers
// to resume. Only uuids + verdicts are stored — never image data.
const REVIEW_SAVE_KEY = "pc-review-v1";
function saveReviewState() {
  if (state.view !== "review" || state.finalize === "done") return;
  try {
    localStorage.setItem(REVIEW_SAVE_KEY, JSON.stringify({
      manual: state.manual, layers: reviewLayers(), decisions: state.decisions,
      dates: state.reviewDates || { since: null, until: null }, savedAt: Date.now(),
    }));
  } catch { /* storage unavailable — persistence is best-effort */ }
}
function loadReviewState() {
  try {
    const d = JSON.parse(localStorage.getItem(REVIEW_SAVE_KEY) || "null");
    if (!d || !d.decisions || Date.now() - (d.savedAt || 0) > 7 * 864e5) return null;
    const items = Object.values(d.decisions).reduce((s, m) => s + Object.keys(m).length, 0);
    return items ? { ...d, items } : null;
  } catch { return null; }
}
function clearReviewState() { try { localStorage.removeItem(REVIEW_SAVE_KEY); } catch {} }

/* ---- in-page error banner (replaces blocking alert()s) --------------------- */
let flashRetry = null;    // closure re-running the failed action (not serializable state)
function showFlash(title, msg, retry) {
  state.flash = { title, msg };
  flashRetry = retry || null;
  state.view = "home";
  if (state.phase === "scanning") state.phase = "idle";
  render();
}

/* ---- chrome --------------------------------------------------------------- */
// The status dot/text reflect REAL state: grey until we've read the library, green
// once connected, red if access failed.
function libStatusBits() {
  if (state.libStatus === "error") return { dot: "err", text: "Library access needed" };
  if (state.libStatus === "connected") {
    return { dot: "ok", text: state.lib
      ? `Library connected · ${fmtN(state.lib.photos)} photos · ${fmtN(state.lib.videos)} videos`
      : "Library connected" };
  }
  return { dot: "idle", text: "Library not connected" };
}
// Update the top-bar status in place (no full re-render) — e.g. mid-scan, the
// moment the library connection is established.
function syncChromeStatus() {
  const st = $(".topbar .status"); if (!st) return;
  const { dot, text } = libStatusBits();
  st.innerHTML = `<span class="dot ${dot}"></span>${text}`;
}
function chrome(inner) {
  const { dot, text } = libStatusBits();
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
    const photosErr = (state.errorMsg || "").includes("photos-access");
    const errTtl = photosErr ? "Photos access needed" : "Couldn't read your photo library";
    const errMsg = photosErr
      ? `Library Cleanup needs permission to manage your <b>Photos</b> so it can remove the
         items you pick. Open System Settings ▸ Privacy &amp; Security ▸ <b>Photos</b>, enable
         <b>Library Cleanup</b>, then click Analyze again.`
      : `Library Cleanup needs <b>Full Disk Access</b>. Open System Settings ▸ Privacy &amp;
         Security ▸ <b>Full Disk Access</b>, enable <b>Library Cleanup</b>, then click Analyze again.`;
    const err = state.libStatus === "error" ? `
        <div class="errbox">
          <div class="errttl">${errTtl}</div>
          <div class="errmsg">${errMsg}</div>
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
        ${state.flash ? `
        <div class="errbox">
          <div class="errttl">${escapeHtml(state.flash.title)}</div>
          <div class="errmsg">${escapeHtml(state.flash.msg || "")}</div>
          <div class="errrow">
            ${flashRetry ? `<button class="btn btn-primary sm" id="flashRetry">Try again</button>` : ""}
            <button class="btn-secondary sm" id="flashDismiss">Dismiss</button>
          </div>
        </div>` : ""}
        ${(() => {
          const sv = loadReviewState();
          return sv ? `
        <div class="resume-box">
          <div class="resume-txt"><b>Unfinished review</b> — ${fmtN(sv.items)} decision${sv.items === 1 ? "" : "s"} saved${sv.manual ? " (manual review)" : ""}.</div>
          <button class="btn btn-primary sm" id="resumeReview">Resume review</button>
          <button class="btn-secondary sm" id="discardReview">Discard</button>
        </div>` : "";
        })()}
        <button class="btn btn-primary" id="analyze">Analyze Library</button>
        <div class="past">Takes a minute. No commitment — nothing's deleted until you say so.</div>
      </div></div></div>
      <div class="foot-note">
        <span class="fn-msg">${icon("i-lock")} Runs entirely on your Mac. Your photos never go anywhere.</span>
        <span class="fn-sep"></span>
        <button class="support-link" id="supportFooter" title="Support Library Cleanup on Ko-fi">${icon("i-coffee")} Buy me a coffee</button>
      </div>`;
  } else if (state.phase === "scanning") {
    body = `
      <div class="scroll"><div class="home"><div class="scanning">
        <div class="spinner"></div>
        <h2>Analyzing your library…</h2>
        <div class="step" id="step">Starting…</div>
        <div class="progress" id="prog"><span id="bar" style="width:0"></span></div>
        <div class="count" id="count"></div>
        <div style="margin-top:22px"><button class="btn-secondary" id="cancel">Cancel</button></div>
      </div></div></div>`;
  } else {
    body = categoriesBody();
  }
  app.innerHTML = chrome(body) + (state.update ? updateModalHtml() : "");
  bindHome();
}

/* ---- self-update prompt ---------------------------------------------------- */
function updateModalHtml() {
  const u = state.update || {};
  if (state.updateStatus === "working" || state.updateStatus === "relaunching") {
    const j = state.updateJob || {};
    const relaunching = state.updateStatus === "relaunching" || j.status === "relaunching";
    const pct = j.frac != null ? Math.round(j.frac * 100) : null;
    const msg = relaunching ? "Relaunching…" : (j.message || "Downloading update…");
    const bar = (j.status === "downloading" && pct != null)
      ? `<div class="progress" style="margin:2px auto 0"><span id="u-bar" style="width:${pct}%"></span></div>`
      : `<div class="progress indet" style="margin:2px auto 0"><span style="width:100%"></span></div>`;
    return `<div class="backdrop"><div class="modal center">
      <h3>${relaunching ? "Updating Library Cleanup" : `Updating to v${escapeHtml(u.latest || "")}`}</h3>
      <div class="working"><div class="spinner sm"></div>
        <div id="u-msg" style="color:var(--pc-text-secondary)">${escapeHtml(msg)}${pct != null && !relaunching ? ` · ${pct}%` : ""}</div>
        ${bar}
        <div style="font-size:12px;color:var(--pc-text-tertiary)">${relaunching ? "The app will reopen in a moment." : "Please keep the app open until it relaunches."}</div>
      </div></div></div>`;
  }
  if (state.updateStatus === "error") {
    return `<div class="backdrop"><div class="modal center">
      <div class="done-disc" style="background:var(--pc-warn)">${icon("i-x")}</div>
      <h3>Update failed</h3>
      <p class="head-n">${escapeHtml(state.updateErr || "Something went wrong.")} You can try again or download it manually.</p>
      <div class="actions" style="justify-content:center">
        <button class="btn-secondary" id="u-page">Open release page</button>
        <button class="btn btn-primary" id="u-retry">Try again</button>
      </div></div></div>`;
  }
  // prompt
  return `<div class="backdrop"><div class="modal">
    <h3>Update available</h3>
    <p class="head-n">A newer version of Library Cleanup is ready.
      <b>v${escapeHtml(u.current || "")}</b> → <b>v${escapeHtml(u.latest || "")}</b>${u.size ? ` · ${fmtSave(u.size)}` : ""}</p>
    <div class="row" style="font-size:12px;color:var(--pc-text-tertiary);margin:6px 0 16px">Downloads, installs, and relaunches automatically. Only the update is fetched — your photos never leave this Mac.</div>
    <div class="actions">
      <button class="btn-secondary" id="u-later">Later</button>
      <button class="btn btn-primary" id="u-go">Download &amp; install</button>
    </div></div></div>`;
}

function bindUpdate() {
  const later = $("#u-later"); if (later) later.onclick = () => { state.update = null; render(); };
  const go = $("#u-go"); if (go) go.onclick = startUpdate;
  const retry = $("#u-retry"); if (retry) retry.onclick = startUpdate;
  const page = $("#u-page"); if (page) page.onclick = () => api.post("/api/update/open-page").catch(() => {});
}

async function startUpdate() {
  state.updateStatus = "working";
  state.updateJob = { status: "downloading", message: "Starting…" };
  state.updateErr = "";
  render();
  let res;
  try { res = await api.post("/api/update/apply"); }
  catch (e) { state.updateStatus = "error"; state.updateErr = e.message; return void render(); }
  if (res && res.started === false) {
    if (res.can_install === false) { api.post("/api/update/open-page").catch(() => {}); state.update = null; return void render(); }
    state.updateStatus = "error";
    state.updateErr = res.error || "Update is no longer available.";
    return void render();
  }
  pollUpdate();
}

async function pollUpdate() {
  let s;
  try { s = await api.get("/api/update/status"); }
  catch { return void setTimeout(pollUpdate, 800); }   // server may be relaunching
  const prev = state.updateJob || {};
  state.updateJob = s;
  if (s.status === "error") { state.updateStatus = "error"; state.updateErr = s.error || "Update failed."; return void render(); }
  if (s.status === "relaunching") { state.updateStatus = "relaunching"; render(); return; }  // app will quit & reopen
  // Within a steady phase only the numbers move — patch them in place so the
  // spinner and progress-bar CSS animations keep running instead of restarting
  // on every poll. Fall back to a full render on any phase transition.
  if (!patchUpdateProgress(prev)) render();
  setTimeout(pollUpdate, 500);
}

// Returns true if the live progress could be updated without a re-render, i.e.
// the modal is mounted and the bar type (determinate ↔ indeterminate) is
// unchanged. Returns false to signal the caller should do a full render().
function patchUpdateProgress(prev) {
  const msgEl = $("#u-msg");
  if (!msgEl) return false;                                    // modal not mounted
  const j = state.updateJob || {};
  const pct = j.frac != null ? Math.round(j.frac * 100) : null;
  const determinate = j.status === "downloading" && pct != null;
  const wasDeterminate = prev.status === "downloading" && prev.frac != null;
  if (determinate !== wasDeterminate) return false;            // bar element swaps → full render
  msgEl.textContent = (j.message || "Downloading update…") + (pct != null ? ` · ${pct}%` : "");
  if (determinate) { const barEl = $("#u-bar"); if (barEl) barEl.style.width = pct + "%"; }
  return true;
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

// Right-hand cell of a category row. Both empty states render the SAME muted
// pill so the rows line up — only the wording differs: "None identified" when a
// category has nothing in the whole library, "None in range" when it has items
// but none in the current time filter.
function catRightHtml(c, s, f) {
  const identified = s && s.items > 0;
  const has = identified && f.items > 0;
  if (!has) return `<span class="none">${identified ? "None in range" : "None identified"}</span>`;
  const sub = c.grouped ? `across ${fmtN(f.groups)} ${c.setword}` : "flagged to remove";
  return `<div class="count"><span data-count>${fmtN(f.items)}</span> <span style="font-weight:400;color:var(--pc-text-tertiary)">${c.noun}</span></div>
       <div class="save" data-save>Save up to ${fmtSave(f.bytes)}</div>
       <div class="desc"><span data-groups>${sub}</span></div>`;
}
function catCard(c) {
  const s = state.summary && state.summary[c.id];
  const identified = s && s.items > 0;
  const f = filteredCat(c.id);
  const has = identified && f.items > 0;
  const on = state.selected.has(c.id) && has;
  const right = catRightHtml(c, s, f);
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
      <button class="btn-secondary" id="manual" title="Browse every photo &amp; video in this date range; nothing is pre-selected for removal">Review manually</button>
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
    bar.title = `${monthLabel(m)} · up to ${fmtSave(vals[i])} — click to focus this month, then drag a handle to widen`;
    bar.onclick = () => {            // select exactly this one month; both thumbs land here
      state.range = { lo: i, hi: i };
      const rStart = $("#rStart"), rEnd = $("#rEnd");
      if (rStart) rStart.value = i;
      if (rEnd) rEnd.value = i;
      updateFilter();
    };
    bars.appendChild(bar);
  });
  chart.innerHTML = ""; chart.appendChild(bars);
  // Year ticks. Pick a "nice" step (round years) sized to width, anchor each
  // label to the MIDDLE of its year's months (January-anchoring made them look
  // shifted left of their bars), center all labels, then drop any that would
  // collide — the two endpoints always win, so a partial first/last year can't
  // strand a stray label next to the edge.
  const tickFrag = document.createDocumentFragment();
  const years = [...new Set(axis.map((m) => m.slice(0, 4)))], n = years.length;
  const avail = ticks.clientWidth || chart.clientWidth || 640;
  const step = [1, 2, 5, 10, 25, 50, 100].find((s) => Math.ceil(n / s) <= Math.max(2, Math.floor(avail / 58))) || n;
  const denom = axis.length - 1 || 1, HALF = 3.2, MINGAP = 46 / avail * 100;
  const midPct = (yi) => {                       // % of the year's mid-month (months are contiguous)
    let a = axis.findIndex((m) => m.startsWith(years[yi])), b = a;
    while (b + 1 < axis.length && axis[b + 1].startsWith(years[yi])) b++;
    return (a + b) / 2 / denom * 100;
  };
  const cand = new Set([0, n - 1]);
  for (let i = 0; i < n; i++) if (+years[i] % step === 0) cand.add(i);
  let items = [...cand].sort((a, b) => a - b).map((yi) => ({ yi, leftPct: midPct(yi) }));
  if (items.length > 1) {
    items[0].leftPct = Math.max(items[0].leftPct, HALF);                        // keep edges un-clipped
    items[items.length - 1].leftPct = Math.min(items[items.length - 1].leftPct, 100 - HALF);
    const last = items[items.length - 1], kept = [items[0]];                    // greedy; endpoints win
    for (let k = 1; k < items.length - 1; k++)
      if (items[k].leftPct - kept[kept.length - 1].leftPct >= MINGAP) kept.push(items[k]);
    while (kept.length > 1 && last.leftPct - kept[kept.length - 1].leftPct < MINGAP) kept.pop();
    kept.push(last); items = kept;
  }
  items.forEach(({ yi, leftPct }) => {
    const sp = document.createElement("span"); sp.textContent = years[yi];
    sp.style.left = leftPct + "%"; tickFrag.appendChild(sp);
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
    const id = row.dataset.cat, f = filteredCat(id), c = CAT[id], s = state.summary && state.summary[id];
    const rt = $(".right", row); if (rt) rt.innerHTML = catRightHtml(c, s, f);   // same helper as first render
    const identified = s && s.items > 0;
    row.style.opacity = (identified && f.items === 0) ? ".55" : "";
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
  if (state.update) bindUpdate();
  const a = $("#analyze"); if (a) a.onclick = () => startAnalyze();   // full-library scan
  const ol = $("#openlog"); if (ol) ol.onclick = () => api.post("/api/open-log").catch(() => {});
  const sf = $("#supportFooter"); if (sf) sf.onclick = donate;
  const c = $("#cancel"); if (c) c.onclick = () => {
    api.post("/api/cancel").catch(() => {});   // actually stop the scan server-side
    state.cancelled = true; state.phase = "idle"; render();
  };
  // Explicit Re-scan: force a full library re-read (full range, all categories),
  // unlike the fast implicit refreshes after a review / on resume.
  const rs = $("#rescan"); if (rs) rs.onclick = () => { state.range = null; startAnalyze(null, { force: true }); };
  const rv = $("#review"); if (rv) rv.onclick = () => enterReview();
  const mn = $("#manual"); if (mn) mn.onclick = () => enterManualReview();
  const rr = $("#resumeReview"); if (rr) rr.onclick = resumeSavedReview;
  const dr = $("#discardReview"); if (dr) dr.onclick = () => { clearReviewState(); render(); };
  const fr = $("#flashRetry"); if (fr) fr.onclick = () => {
    const retry = flashRetry; state.flash = null; flashRetry = null; retry && retry();
  };
  const fd = $("#flashDismiss"); if (fd) fd.onclick = () => { state.flash = null; flashRetry = null; render(); };
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
    // Derive lo/hi from the min/max of the two thumbs (no clamping). After a
    // single-column click stacks both thumbs on one month, dragging either one
    // then grows a range out from that month instead of being stuck (the old
    // clamp pinned the top thumb against the bottom one).
    const onThumb = () => {
      const a = +rStart.value, b = +rEnd.value;
      state.range = { lo: Math.min(a, b), hi: Math.max(a, b) };
      updateFilter();
    };
    if (rStart) rStart.oninput = onThumb;
    if (rEnd) rEnd.oninput = onThumb;
    const rst = $("#tfReset"); if (rst) rst.onclick = () => {
      state.range = { lo: 0, hi: state.months.length - 1 };
      rStart.value = 0; rEnd.value = state.months.length - 1; updateFilter();
    };
    updateFilter();
  }
}

// `force` re-reads the whole library (the explicit Re-scan action); the implicit
// refreshes — after a review, on resume, first analyze — leave it off so they
// reuse the pruned in-RAM records and return fast.
async function startAnalyze(range, { force = false } = {}) {
  // A full-library scan (no date scope) resets the time filter to "all time":
  // a stale narrow range left over from a previous session must not carry onto
  // the fresh full axis, where buildMonthAxis would keep it and strand the
  // picker on an irrelevant window (e.g. the first two months). Scoped scans
  // (resume) keep their range — they go straight into review, not the picker.
  if (!range || (!range.since && !range.until)) state.range = null;
  state.phase = "scanning";
  state.cancelled = false;
  state.flash = null; flashRetry = null;
  resetScanBar();                 // fresh bar for this scan (starts at 0)
  render();
  let res;
  try {
    res = await api.post("/api/analyze", { layers: CATS.map((c) => c.id),
      since: range?.since ?? null, until: range?.until ?? null, force });
  } catch (e) {
    showFlash("Couldn't start the scan", e.message, () => startAnalyze(range, { force }));
    return;
  }
  if (res && res.started === false) {   // a previous scan is still winding down
    showFlash("A scan is already running",
      "Give it a moment to stop, then try again.", () => startAnalyze(range, { force }));
    return;
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
    resetScanBar();
    state.libStatus = "connected";   // the library was read successfully
    state.summary = p.summary;
    state.selected = new Set(CATS.filter((c) => p.summary[c.id]?.items > 0).map((c) => c.id));
    api.get("/api/library-stats").then((s) => { state.lib = s; render(); }).catch(() => {});
    state.phase = "results";
    const saved = state.pendingResume;                 // resuming a curated review?
    if (saved) {
      state.pendingResume = null;
      state.selected = new Set(saved.layers.filter((l) => CAT[l]));
      await enterReview(saved.decisions, saved.dates);
      return;
    }
    render();
  } else if (p.status === "error") {
    resetScanBar();
    state.libStatus = "error";
    state.errorMsg = p.error || "Something went wrong.";
    state.errorLog = p.log || "";
    state.phase = "idle"; render();
  } else if (p.status === "cancelled") {
    resetScanBar();
    state.phase = "idle"; render();     // scan stopped server-side; back to start
  } else {
    // Library is connected the moment scanning gets past access/connect (real
    // counted progress) — reflect that in the top bar immediately, not at the end.
    if (state.libStatus !== "connected" && (p.total || /^(Analyzing|Detecting|Grouping|Comparing|Finishing)/.test(p.message || ""))) {
      state.libStatus = "connected";
      syncChromeStatus();
    }
    setTimeout(pollProgress, 400);
  }
}

// The scan bar is JS-driven so it's always moving: it eases toward the backend's
// reported fraction, and when the backend goes quiet (the opaque PhotosDB parse,
// or a slow per-face pass) it creeps a little past the last real point rather
// than freezing. `shown` only ever increases, so the bar never jumps backwards.
let scanAnim = null;

function resetScanBar() {
  if (scanAnim && scanAnim.timer) clearInterval(scanAnim.timer);
  scanAnim = null;
}

function updateScanning(p) {
  const step = $("#step"), bar = $("#bar"), count = $("#count"), prog = $("#prog");
  if (!step) return;
  step.textContent = p.message || "Working…";
  count.textContent = p.total ? `${fmtN(p.done)} / ${fmtN(p.total)}` : "";
  if (prog) prog.classList.remove("indet");
  const real = (p.frac != null) ? p.frac : (p.total ? p.done / p.total : null);
  if (!scanAnim) {
    scanAnim = { shown: 0, floor: 0, lastRealAt: Date.now() };
    if (bar) bar.style.transition = "none";     // JS owns the width; no CSS lag
    scanAnim.timer = setInterval(tickScanBar, 65);
  }
  if (real != null) {
    scanAnim.floor = Math.max(scanAnim.floor, real);   // last real point, monotonic
    scanAnim.lastRealAt = Date.now();
  }
}

function tickScanBar() {
  const a = scanAnim;
  if (!a) return;
  const bar = $("#bar");
  if (!bar) { resetScanBar(); return; }          // left the scanning view
  const quiet = Date.now() - a.lastRealAt > 300;
  // Quiet → creep toward a small cap past the last real point (keeps moving);
  // active → ease toward the real point. Either way shown only rises.
  const cap = quiet ? Math.min(0.985, a.floor + 0.05) : a.floor;
  const ease = quiet ? 0.05 : 0.18;
  a.shown = Math.min(0.995, a.shown + Math.max(0, cap - a.shown) * ease);
  bar.style.width = (a.shown * 100).toFixed(1) + "%";
}

/* ---- review --------------------------------------------------------------- */
// since/until (YYYY-MM-DD) for the current time-filter range, or nulls if "all".
function rangeDates() {
  if (!state.months.length || rangeIsAll()) return { since: null, until: null };
  const lo = state.months[state.range.lo], hi = state.months[state.range.hi];
  const [hy, hm] = hi.split("-").map(Number);
  const lastDay = new Date(hy, hm, 0).getDate();
  return { since: `${lo}-01`, until: `${hi}-${String(lastDay).padStart(2, "0")}` };
}

// Resume a saved review after a quit/crash. Manual feeds reload directly; a
// curated review needs candidates first, so re-run the (scoped) scan and merge
// the saved decisions once it lands (see pollProgress).
async function resumeSavedReview() {
  const saved = loadReviewState();
  if (!saved) { render(); return; }
  if (saved.manual) {
    await enterManualReview(saved.dates, saved.decisions.all || {});
  } else {
    state.pendingResume = saved;
    startAnalyze(saved.dates);
  }
}

// Manual review: every photo & video in range as one chronological feed, all kept.
// The feed is PAGED: the first ALL_PAGE items load here; further pages stream in
// from the scroll sentinel (fetchMoreAllItems). `dates`/`savedDecisions` are only
// passed when resuming a saved review.
async function enterManualReview(dates, savedDecisions) {
  state.view = "review"; state.manual = true;
  state.candidates = {}; state.decisions = {};
  state.allTotal = 0; state.allLoading = false; state.allDefault = "keep";
  pvCache.clear();
  app.innerHTML = chrome(`<div class="scroll"><div class="review"><div class="scanning">
      <div class="spinner"></div><h2>Loading your photos…</h2></div></div></div>`);
  const { since, until } = dates || rangeDates();
  state.reviewDates = { since, until };     // what saveReviewState records
  const qs = new URLSearchParams();
  if (since) qs.set("since", since);
  if (until) qs.set("until", until);
  qs.set("limit", String(ALL_PAGE));
  try {
    const res = await api.get(`/api/all-items?${qs.toString()}`);
    state.candidates.all = res.groups;
    state.allTotal = res.total ?? (res.groups[0]?.photos.length || 0);
    // Seed decisions with the FULL saved overlay first (it may cover items on
    // pages not fetched yet — their verdicts must survive a partial reload; any
    // uuid no longer in the library comes back "unmatched" from delete, which the
    // done-screen reports). Then default every loaded item without a verdict to keep.
    const d = savedDecisions ? { ...savedDecisions } : {};
    res.groups.forEach((g) => g.photos.forEach((p) => { if (!(p.uuid in d)) d[p.uuid] = "keep"; }));
    state.decisions.all = d;
  } catch (e) {
    state.manual = false;
    showFlash("Couldn't load photos", e.message, () => enterManualReview(dates, savedDecisions));
    return;
  }
  state.selUuid = null; state.selLayer = null; state.pvCollapsed = false;
  renderReview();
  saveReviewState();
}

async function enterReview(savedDecisions, dates) {
  state.view = "review"; state.manual = false;
  state.reviewDates = dates || rangeDates();     // what saveReviewState records
  state.candidates = {};
  state.decisions = {};
  pvCache.clear();                 // drop cached preview images from the previous review
  app.innerHTML = chrome(`<div class="scroll"><div class="review"><div class="scanning">
      <div class="spinner"></div><h2>Loading review…</h2></div></div></div>`);
  const layers = CATS.map((c) => c.id).filter((id) => state.selected.has(id));
  const all = rangeIsAll();
  const loM = all ? null : state.months[state.range.lo];
  const hiM = all ? null : state.months[state.range.hi];
  let results;
  try {   // the layers are independent — fetch them in parallel, not one-by-one
    results = await Promise.all(layers.map((l) => api.get(`/api/candidates?layer=${l}`)));
  } catch (e) {
    showFlash("Couldn't load review", e.message, () => enterReview(savedDecisions, dates));
    return;
  }
  layers.forEach((layer, li) => {
    let groups = results[li].groups;
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
      // Always start from the FRESH algorithm suggestion. We deliberately do NOT
      // replay p.decided (persisted server verdicts) here: a stale discard from a
      // prior aborted round would otherwise silently override a photo the model
      // now recommends keeping (audit #9). In-session resume is handled by the
      // localStorage overlay below, which is scoped to the actual saved review.
      d[p.uuid] = p.suggested_keep ? "keep" : "remove";
    }));
    if (savedDecisions && savedDecisions[layer]) {   // resume: overlay saved verdicts
      for (const [u, v] of Object.entries(savedDecisions[layer])) if (u in d) d[u] = v;
    }
    state.decisions[layer] = d;
  });
  state.selUuid = null; state.selLayer = null; state.pvCollapsed = false;
  renderReview();
  saveReviewState();
}

/* ---- review counters (kept incrementally — no full recount per click) ------ */
// Built once per review load (the authoritative recount); every keep/remove then
// adjusts these in O(1) via decideOne. Chunks rendered later read the same
// state.decisions, so counts stay correct across rendered AND unrendered cards.
function buildReviewIndex() {
  state.idx = {};          // layer -> Map(uuid -> {p, gkey}) for O(1) photo lookup
  state.groupKeep = {};    // layer -> Map(group_key -> keep count) (grouped layers)
  state.layerTally = {};   // layer -> {items, keep, rem} for the section summary
  state.tally = { keep: 0, rem: 0, bytes: 0, items: 0 };
  for (const layer of reviewLayers()) {
    const m = new Map(), gk = new Map();
    for (const g of state.candidates[layer] || [])
      for (const p of g.photos) m.set(p.uuid, { p, gkey: g.group_key });
    const lt = { items: 0, keep: 0, rem: 0 };
    const d = state.decisions[layer] || {};
    // Tally decision entries (like the old counts()): in the manual feed this can
    // include resumed verdicts for pages not fetched yet — their bytes are added
    // when the page arrives (ingestAllPhotos).
    for (const [uuid, v] of Object.entries(d)) {
      lt.items++; state.tally.items++;
      if (v === "keep") { lt.keep++; state.tally.keep++; }
      else { lt.rem++; state.tally.rem++; state.tally.bytes += (m.get(uuid)?.p.bytes || 0); }
    }
    if (CAT[layer].grouped) {
      for (const g of state.candidates[layer] || []) {
        let k = 0;
        for (const p of g.photos) if (d[p.uuid] === "keep") k++;
        gk.set(g.group_key, k);
      }
    }
    state.idx[layer] = m; state.groupKeep[layer] = gk; state.layerTally[layer] = lt;
  }
}

// Flip one decision in STATE and adjust every running counter. Returns true if
// the verdict actually changed. DOM patching is separate (updateCardDom) so bulk
// actions can update decisions for cards that aren't rendered yet.
function decideOne(layer, uuid, v) {
  const d = state.decisions[layer];
  if (!d) return false;
  const old = d[uuid];
  if (old === v) return false;
  d[uuid] = v;
  const ent = state.idx[layer] && state.idx[layer].get(uuid);
  const bytes = (ent && ent.p.bytes) || 0;
  const t = state.tally, lt = state.layerTally[layer];
  if (old === undefined) { t.items++; if (lt) lt.items++; }        // brand-new entry
  else if (old === "keep") { t.keep--; if (lt) lt.keep--; }
  else { t.rem--; if (lt) lt.rem--; t.bytes -= bytes; }
  if (v === "keep") { t.keep++; if (lt) lt.keep++; }
  else { t.rem++; if (lt) lt.rem++; t.bytes += bytes; }
  const gm = state.groupKeep[layer];
  if (ent && gm && gm.has(ent.gkey)) {
    if (old === "keep" && v !== "keep") gm.set(ent.gkey, gm.get(ent.gkey) - 1);
    else if (old !== "keep" && v === "keep") gm.set(ent.gkey, gm.get(ent.gkey) + 1);
  }
  return true;
}

function counts() {
  const t = state.tally || { keep: 0, rem: 0, bytes: 0, items: 0 };
  return { keep: t.keep, rem: t.rem, bytes: t.bytes, items: t.items };
}

/* ---- progressive (chunked) group rendering ---------------------------------
   The review grid renders in chunks of ~CHUNK_CARDS cards: the first chunk
   synchronously, the rest as an IntersectionObserver sentinel nears the viewport.
   Grouped layers append whole groups; flat layers (screenshots / expired / the
   manual feed) append card slices into one .gbody. The manual feed additionally
   fetches further server pages when the rendered edge nears the loaded edge.
   Append-only: nothing above the viewport is ever unmounted. */
const CHUNK_CARDS = 250;          // ~cards appended per chunk (groups render whole)
const ALL_PAGE = 1000;            // manual feed: items fetched per server page
let chunkCur = null;              // render cursor {li: layer idx, gi: group idx, pi: photo idx}
let rvIO = null;                  // the sentinel's IntersectionObserver

function morePagesLeft() {
  if (!state.manual) return false;
  const g = state.candidates.all && state.candidates.all[0];
  return !!g && g.photos.length < (state.allTotal || 0);
}

// True when more content can be rendered RIGHT NOW (loaded but not yet in the DOM).
// Waiting on a server page is not "work" — the page's arrival re-pokes the sentinel.
function chunkHasWork() {
  if (!chunkCur) return false;
  const layers = reviewLayers();
  for (let li = chunkCur.li; li < layers.length; li++) {
    const layer = layers[li], groups = state.candidates[layer] || [];
    if (CAT[layer].grouped) {
      if ((li === chunkCur.li ? chunkCur.gi : 0) < groups.length) return true;
    } else {
      const g = groups[0], start = li === chunkCur.li ? chunkCur.pi : 0;
      if (g && start < g.photos.length) return true;
    }
  }
  return false;
}

function ensureSection(grid, layer) {
  let sec = grid.querySelector(`.section[data-layer="${layer}"]`);
  if (!sec) {
    grid.insertAdjacentHTML("beforeend", sectionShellHtml(layer));
    sec = grid.lastElementChild;
  }
  return sec;
}

// Fit-to-aspect wiring for freshly appended thumbs only (data-fit marks done ones).
function wireThumbs(root) {
  if (!root) return;
  root.querySelectorAll("img:not([data-fit])").forEach((img) => {
    img.dataset.fit = "1";
    if (img.complete && img.naturalWidth) fitAspect(img);
    else img.addEventListener("load", () => fitAspect(img), { once: true });
  });
}

// Append the next ~budget cards worth of DOM. Returns the number appended.
function renderChunk(budget = CHUNK_CARDS) {
  const grid = app.querySelector(".rv-main .review");
  if (!grid || !chunkCur) return 0;
  const layers = reviewLayers();
  let added = 0;
  while (added < budget && chunkCur.li < layers.length) {
    const layer = layers[chunkCur.li];
    const groups = state.candidates[layer] || [];
    if (CAT[layer].grouped) {
      if (chunkCur.gi >= groups.length) { chunkCur.li++; chunkCur.gi = 0; chunkCur.pi = 0; continue; }
      const sec = ensureSection(grid, layer);
      const g = groups[chunkCur.gi++];
      sec.insertAdjacentHTML("beforeend", groupHtml(layer, g));
      wireThumbs(sec.lastElementChild);
      added += g.photos.length;
    } else {
      const g = groups[0];
      if (!g || chunkCur.pi >= g.photos.length) {
        if (g && layer === "all" && morePagesLeft()) break;   // wait for the next server page
        chunkCur.li++; chunkCur.gi = 0; chunkCur.pi = 0; continue;
      }
      const sec = ensureSection(grid, layer);
      let body = sec.querySelector(".gbody");
      if (!body) {
        sec.insertAdjacentHTML("beforeend", flatShellHtml(layer, g));
        body = sec.querySelector(".gbody");
      }
      const end = Math.min(g.photos.length, chunkCur.pi + (budget - added));
      body.insertAdjacentHTML("beforeend",
        g.photos.slice(chunkCur.pi, end).map((p) => cardHtml(layer, p)).join(""));
      wireThumbs(body);
      added += end - chunkCur.pi;
      chunkCur.pi = end;
    }
  }
  if (!chunkHasWork() && !morePagesLeft()) {      // fully rendered — retire the sentinel
    const sent = $("#rvSentinel"); if (sent) sent.remove();
    if (rvIO) { rvIO.disconnect(); rvIO = null; }
  }
  return added;
}

// Re-observing forces a fresh intersection check, so chunks keep appending until
// the sentinel finally scrolls out of the (rootMargin-expanded) viewport.
function pokeSentinel() {
  const sent = $("#rvSentinel");
  if (rvIO && sent) { rvIO.unobserve(sent); rvIO.observe(sent); }
}

// Render chunks until the grid extends comfortably past the current scroll
// position (and top up the manual feed's local buffer). Called from both the
// sentinel observer and the scroll fallback — bounded, cheap when nothing to do.
function topUpReview() {
  if (state.view !== "review") return;
  const sc = app.querySelector("#rvMain .scroll");
  if (!sc) return;
  let guard = 0;
  while (guard++ < 40 && chunkHasWork() &&
         sc.scrollHeight - sc.scrollTop - sc.clientHeight < 1600) {
    renderChunk();
  }
  maybeFetchMore();
}

function setupSentinel() {
  if (rvIO) { rvIO.disconnect(); rvIO = null; }
  const sc = app.querySelector("#rvMain .scroll");
  const sent = $("#rvSentinel");
  if (!sc) return;
  // Scroll fallback (and fast path): fills as the user approaches the bottom even
  // if the IntersectionObserver is late; no-ops once everything is rendered.
  sc.addEventListener("scroll", topUpReview, { passive: true });
  if (!sent) return;
  if (!chunkHasWork() && !morePagesLeft()) { sent.remove(); return; }
  rvIO = new IntersectionObserver((entries) => {
    if (state.view !== "review") return;
    if (!entries.some((e) => e.isIntersecting)) return;
    topUpReview();
    if (chunkHasWork()) pokeSentinel();
  }, { root: sc, rootMargin: "1600px 0px" });
  rvIO.observe(sent);
}

// Manual feed: keep at least a few chunks of unrendered items buffered locally.
function maybeFetchMore() {
  if (!state.manual || state.allLoading || !morePagesLeft()) return;
  const g = state.candidates.all && state.candidates.all[0];
  if (!g) return;
  if (g.photos.length - (chunkCur ? chunkCur.pi : 0) < CHUNK_CARDS * 3) fetchMoreAllItems();
}

async function fetchMoreAllItems() {
  if (state.allLoading || !state.manual) return;
  const g = state.candidates.all && state.candidates.all[0];
  if (!g || g.photos.length >= (state.allTotal || 0)) return;
  state.allLoading = true;
  const { since, until } = state.reviewDates || {};
  const qs = new URLSearchParams();
  if (since) qs.set("since", since);
  if (until) qs.set("until", until);
  qs.set("offset", String(g.photos.length));
  qs.set("limit", String(ALL_PAGE));
  try {
    const res = await api.get(`/api/all-items?${qs.toString()}`);
    state.allTotal = res.total ?? state.allTotal;
    const more = (res.groups[0] && res.groups[0].photos) || [];
    if (!more.length) state.allTotal = g.photos.length;   // server ran dry — stop asking
    else {
      ingestAllPhotos(more);
      updateSectionSummary("all");
      updateBars();
    }
    state.allLoading = false;
    topUpReview();                     // resume rendering right away if near the edge
    pokeSentinel();
  } catch {
    state.allLoading = false;
    setTimeout(pokeSentinel, 2000);    // transient failure — retry via the sentinel
  }
}

// Fold a freshly fetched manual page into state: photos list, index, decisions
// (resume overlay wins; otherwise the current bulk default), and running tallies.
function ingestAllPhotos(photos) {
  const g = state.candidates.all[0], d = state.decisions.all;
  const m = state.idx.all, t = state.tally, lt = state.layerTally.all;
  for (const p of photos) {
    g.photos.push(p);
    m.set(p.uuid, { p, gkey: g.group_key });
    if (p.uuid in d) {
      // resumed verdict tallied at build time with unknown size — add bytes now
      if (d[p.uuid] !== "keep") t.bytes += p.bytes || 0;
    } else {
      const v = state.allDefault || "keep";
      d[p.uuid] = v;
      t.items++; lt.items++;
      if (v === "keep") { t.keep++; lt.keep++; }
      else { t.rem++; lt.rem++; t.bytes += p.bytes || 0; }
    }
  }
}

function renderReview() {
  const layers = reviewLayers();
  buildReviewIndex();                    // authoritative recount on (re)load
  const c = counts();
  const shownItems = state.manual ? Math.max(c.items, state.allTotal || 0) : c.items;
  const pct = c.items ? Math.round(((c.keep + c.rem) / c.items) * 100) : 0;
  const body = `
    <div class="bar top">
      <span id="itemsLbl">${fmtN(shownItems)} items in ${layers.length} categor${layers.length === 1 ? "y" : "ies"}</span>
      <span class="mini-prog"><span style="width:${pct}%"></span></span>
      <span><span class="keep-n">Keeping ${fmtN(c.keep)}</span> · <span class="rem-n">Removing ${fmtN(c.rem)}</span></span>
      <span class="sizer" title="Preview size (⌘ + scroll)">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><rect x="3" y="3" width="8" height="8" rx="1.5"/><rect x="13" y="3" width="8" height="8" rx="1.5"/><rect x="3" y="13" width="8" height="8" rx="1.5"/><rect x="13" y="13" width="8" height="8" rx="1.5"/></svg>
        <input type="range" min="84" max="240" step="2" value="${state.cardSize}" id="cardsize">
      </span>
      <span class="bulk">
        <button class="btn-secondary sm" id="keepAll" title="Keep every suggestion in the review">Keep all</button>
        <button class="btn-secondary sm" id="removeAll" title="Remove every suggestion in the review">Remove all</button>
      </span>
    </div>
    <div class="rv-main ${state.pvCollapsed ? "collapsed" : ""}" id="rvMain">
      <div class="scroll"><div class="review"></div><div id="rvSentinel" style="height:1px"></div></div>
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
        <span class="rem-n">Removing ${fmtN(c.rem)}</span> · Frees <span class="frees-n">${fmtSave(c.bytes)}</span></div>
      <button class="btn btn-primary" id="finalize" ${c.rem ? "" : "disabled"}>Review &amp; Finalize</button>
    </div>`;
  app.innerHTML = chrome(body) + (state.finalize ? modalHtml() : "");
  chunkCur = { li: 0, gi: 0, pi: 0 };
  const grid = app.querySelector(".rv-main .review");
  layers.forEach((l) => ensureSection(grid, l));   // headers up front (even empty layers)
  renderChunk();                         // first chunk synchronously — instant paint
  setupSentinel();                       // the rest streams in as you scroll
  bindReview();
}

// Section shell only (h3 + live summary); groups/cards are appended by renderChunk.
function sectionShellHtml(layer) {
  const c = CAT[layer];
  const lt = state.layerTally[layer] || { items: 0, keep: 0, rem: 0 };
  const groups = state.candidates[layer] || [];
  const summary = sectionSummary(layer, groups.length, lt.items, lt.keep, lt.rem);
  return `<div class="section" data-layer="${layer}"><h3>${c.name}</h3><div class="summary">${summary}</div></div>`;
}

function groupHtml(layer, g) {
  const noun = layer === "videos" ? "clips" : "shots";
  const d = state.decisions[layer];
  const gk = state.groupKeep[layer];
  const keep = gk && gk.has(g.group_key) ? gk.get(g.group_key)
    : g.photos.filter((p) => d[p.uuid] === "keep").length;
  const rem = g.photos.length - keep;
  const collapsed = state.collapsed.has(g.group_key);
  const gkey = escapeHtml(g.group_key);
  return `<div class="group ${collapsed ? "collapsed" : ""}" data-group="${gkey}"
              data-layer="${layer}" data-noun="${noun}" data-size="${g.size}">
    <div class="ghead">
      <span class="gtitle">${escapeHtml(g.title)}</span>
      <span class="gmeta">· ${g.size} ${noun} · keep ${keep} · remove ${rem}</span>
      <span class="spacer"></span>
      <button class="btn-secondary sm" data-all="keep" data-layer="${layer}" data-g="${gkey}">Keep all</button>
      <button class="btn-secondary sm" data-all="remove" data-layer="${layer}" data-g="${gkey}">Remove all</button>
      <button class="chev" data-collapse="${gkey}">${collapsed ? "▸" : "▾"}</button>
    </div>
    <div class="gbody">${g.photos.map((p) => cardHtml(layer, p)).join("")}</div>
  </div>`;
}

// Summary line for a section — grouped layers count sets, the manual feed counts
// items, the flat curated layers say "flagged".
function sectionSummary(layer, nGroups, items, keep, rem) {
  const c = CAT[layer];
  if (c.grouped) return `${fmtN(nGroups)} ${c.setword} · keeping ${keep} · removing ${rem}`;
  if (layer === "all") {
    const total = state.allTotal || 0;   // paged feed: show progress while loading
    const n = total > items ? `${fmtN(items)} of ${fmtN(total)} loaded` : fmtN(items);
    return `${n} items · keeping ${keep} · removing ${rem}`;
  }
  return `${fmtN(items)} flagged · keeping ${keep} · removing ${rem}`;
}

// Flat-layer shell: header only — renderChunk appends card slices into the .gbody.
function flatShellHtml(layer, g) {
  const manual = layer === "all";
  const title = manual ? "All photos &amp; videos" : "All flagged to remove";
  const meta = manual ? "· chronological · tap ✕ to remove" : "· tap any to keep";
  const gkey = escapeHtml(g.group_key);
  return `<div class="group"><div class="ghead">
      <span class="gtitle">${title}</span><span class="gmeta">${meta}</span>
      <span class="spacer"></span>
      <button class="btn-secondary sm" data-all="keep" data-layer="${layer}" data-g="${gkey}">Keep all</button>
      <button class="btn-secondary sm" data-all="remove" data-layer="${layer}" data-g="${gkey}">Remove all</button>
    </div><div class="gbody"></div></div>`;
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
  // Cell sized to the image's aspect (uncropped): long side = --card, short side
  // scaled by --fw/--fh. Estimated from metadata; corrected from the loaded thumb.
  const w = p.width || 1, h = p.height || 1;
  const fw = w >= h ? 1 : w / h, fh = w >= h ? h / w : 1;
  return `<div class="card ${shot ? "shot" : ""} ${v === "keep" ? "keep" : "remove"}" data-uuid="${escapeHtml(p.uuid)}" data-layer="${escapeHtml(layer)}" style="--fw:${fw.toFixed(4)};--fh:${fh.toFixed(4)}">
    <div class="frame" tabindex="0" role="button" aria-pressed="${v === "keep"}">
      <img src="${escapeHtml(p.thumb)}" loading="lazy" decoding="async" alt="">
      ${fav}${overlay}${badge}
    </div>
    <div class="fn">${escapeHtml(p.filename)} · ${fmtSize(p.size_mb)}</div></div>`;
}

// Correct a card's aspect from the actually-rendered (EXIF-applied) thumb, in case
// the metadata dimensions were pre-rotation. No-op when the estimate already matches.
function fitAspect(img) {
  const w = img.naturalWidth, h = img.naturalHeight;
  if (!w || !h) return;
  const card = img.closest(".card"); if (!card) return;
  const fw = w >= h ? 1 : w / h, fh = w >= h ? h / w : 1;
  if (Math.abs(parseFloat(card.style.getPropertyValue("--fw") || 1) - fw) > 0.02 ||
      Math.abs(parseFloat(card.style.getPropertyValue("--fh") || 1) - fh) > 0.02) {
    card.style.setProperty("--fw", fw.toFixed(4));
    card.style.setProperty("--fh", fh.toFixed(4));
  }
}

function escapeHtml(s) {
  return (s || "").replace(/[&<>"']/g, (m) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[m]));
}

function bindReview() {
  // (thumbnail aspect wiring happens per appended chunk — see wireThumbs)
  // No summary happens after a cold-start resume — land on the intro, not an empty picker.
  $("#back").onclick = () => {
    if (saveTimer) { clearTimeout(saveTimer); saveTimer = null; saveReviewState(); }  // flush pending save
    if (rvIO) { rvIO.disconnect(); rvIO = null; }
    state.view = "home"; state.phase = state.summary ? "results" : "idle"; state.manual = false; render();
  };
  const fin = $("#finalize"); if (fin) fin.onclick = openFinalize;
  const cs = $("#cardsize"); if (cs) cs.oninput = (e) => setCardSize(+e.target.value);

  // Whole-review keep/remove all. Operates on STATE (every decided item, incl.
  // cards in chunks not rendered yet), then patches only the rendered cards.
  const setAll = (v) => {
    if (state.manual) state.allDefault = v;    // items on future pages follow the bulk verdict
    for (const layer of reviewLayers()) {
      for (const uuid of Object.keys(state.decisions[layer] || {})) decideOne(layer, uuid, v);
    }
    app.querySelectorAll(".card[data-uuid]").forEach((card) => updateCardDom(card, v));
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
      // Group-level keep/remove all: state first (covers cards not rendered yet —
      // the manual/flat feed's group spans every loaded item), then the DOM.
      const layer = all.dataset.layer, v = all.dataset.all;
      const g = (state.candidates[layer] || []).find((x) => x.group_key === all.dataset.g);
      if (g) {
        if (state.manual && layer === "all") state.allDefault = v;
        for (const p of g.photos) decideOne(layer, p.uuid, v);
      }
      const gEl = all.closest(".group");
      gEl.querySelectorAll(".card[data-uuid]").forEach((card) => updateCardDom(card, v));
      if (state.selUuid && gEl.querySelector(`.card[data-uuid="${CSS.escape(state.selUuid)}"]`)) fillPreview();
      updateGroupMeta(gEl);
      updateSectionSummary(layer);
      updateBars();
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

  // Size the preview to the responsive default for the current window, unless the
  // user has manually dragged it this session.
  if (!state.pvUserSized && !state.pvCollapsed) setPreviewWidth(defaultPreviewWidth());

  // drag the divider to resize the grid / preview split (double-click resets)
  const rz = $("#pvResize");
  if (rz) {
    rz.onpointerdown = (e) => {
      e.preventDefault();
      const rm = $("#rvMain"); rm.classList.add("resizing");
      try { rz.setPointerCapture(e.pointerId); } catch {}
      const startX = e.clientX, startW = state.pvWidth, maxW = rm.clientWidth - 320;
      const onMove = (ev) => {
        state.pvUserSized = true;                    // manual size wins over auto-sizing
        setPreviewWidth(Math.min(maxW, startW + (startX - ev.clientX)));  // drag left = wider
      };
      const onUp = () => {
        rm.classList.remove("resizing");
        rz.removeEventListener("pointermove", onMove);
        rz.removeEventListener("pointerup", onUp);
        try { rz.releasePointerCapture(e.pointerId); } catch {}
      };
      rz.addEventListener("pointermove", onMove);
      rz.addEventListener("pointerup", onUp);
    };
    rz.ondblclick = () => { state.pvUserSized = false; setPreviewWidth(defaultPreviewWidth()); };  // reset to the responsive default
  }

  // initialise / restore the previewed card so arrows + Space work the moment you arrive
  if (!state.finalize) {
    let card = selectedCardEl() || app.querySelector(".group:not(.collapsed) .card[data-uuid]");
    if (card) selectCard(card, { scroll: false });
  }

  // finalize modal buttons (present when renderReview ran with a modal open)
  if (state.finalize) bindModal();
}

// Swap ONLY the finalize modal overlay in/out — the review grid (and its
// progressively rendered chunks + scroll position) stays untouched.
function syncModal() {
  const bd = app.querySelector(".backdrop");
  if (bd) bd.remove();
  if (state.finalize) {
    app.insertAdjacentHTML("beforeend", modalHtml());
    bindModal();
  }
}

function bindModal() {
  if (state.finalize === "confirm") {
    $("#m-cancel").onclick = () => { state.finalize = null; state.finalizeErr = null; syncModal(); };
    $("#m-go").onclick = () => { state.finalizeErr = null; doFinalize(); };
  } else if (state.finalize === "done") {
    const wasManual = state.manual;
    const sd = $("#supportDone"); if (sd) sd.onclick = donate;
    const mn = $("#m-new"); if (mn) mn.onclick = () => {
      if (wasManual) {
        // Manual flow: land back on the categories picker with fresh counts.
        // Re-scan the WHOLE library (range null), NOT just the period the user
        // reviewed: a scoped re-scan rebuilds the month axis from only those
        // months, stranding the time filter there with no way to widen back to
        // the full range. The post-delete in-RAM memo is still warm, so a
        // full-range re-scan is nearly as fast as a scoped one anyway.
        Object.assign(state, { view: "home", finalize: null, done: null,
          candidates: {}, decisions: {}, manual: false, range: null });
        startAnalyze(null);
        return;
      }
      Object.assign(state, { view: "home", phase: "idle", finalize: null, done: null,
        candidates: {}, decisions: {}, selected: new Set(), summary: null, manual: false });
      render();
    };
    // unauthorized: keep the review intact, just close the modal
    const mb = $("#m-back"); if (mb) mb.onclick = () => { state.finalize = null; state.done = null; syncModal(); };
  }
}

// DOM half of a decision flip — class, badge, aria. State/tallies live in decideOne.
function updateCardDom(card, v) {
  card.classList.toggle("keep", v === "keep");
  card.classList.toggle("remove", v !== "keep");
  $(".badge", card).innerHTML = icon(v === "keep" ? "i-check" : "i-x");
  $(".frame", card).setAttribute("aria-pressed", v === "keep");
}

function setCardDecision(card, v) {
  const { uuid, layer } = card.dataset;
  decideOne(layer, uuid, v);
  updateCardDom(card, v);
}

function flip(card) {
  const { uuid, layer } = card.dataset;
  setCardDecision(card, state.decisions[layer][uuid] === "keep" ? "remove" : "keep");
  // O(1) per click: patch only this card's group header, its section line, the bars.
  const gEl = card.closest(".group[data-group]");
  if (gEl) updateGroupMeta(gEl);
  updateSectionSummary(layer);
  updateBars();
}

function updateGroupMeta(gEl) {
  if (!gEl || !gEl.dataset.group) return;    // flat groups have a static meta line
  const gk = state.groupKeep[gEl.dataset.layer];
  const keep = gk ? gk.get(gEl.dataset.group) : undefined;
  if (keep === undefined) return;
  const meta = $(".gmeta", gEl);
  if (meta) meta.textContent = `· ${gEl.dataset.size} ${gEl.dataset.noun} · keep ${keep} · remove ${+gEl.dataset.size - keep}`;
}

function updateSectionSummary(layer) {
  const lt = state.layerTally[layer];
  if (!lt) return;
  const el = document.querySelector(`.section[data-layer="${layer}"] .summary`);
  if (el) el.textContent = sectionSummary(layer, (state.candidates[layer] || []).length, lt.items, lt.keep, lt.rem);
}

// Refresh all count text (section summaries, group headers, bars) from the running
// tallies — iterates rendered GROUP HEADERS only, never every card. Used after
// bulk actions; single flips take the O(1) path in flip().
function refreshCounts() {
  for (const layer of reviewLayers()) updateSectionSummary(layer);
  app.querySelectorAll(".group[data-group]").forEach((gEl) => updateGroupMeta(gEl));
  updateBars();
}

/* ---- review preview panel + keyboard selection --------------------------- */
function photoOf(layer, uuid) {
  const ent = state.idx && state.idx[layer] && state.idx[layer].get(uuid);
  if (ent) return ent.p;
  for (const g of (state.candidates[layer] || [])) {   // fallback (index not built)
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
const PV_CACHE_MAX = 5;           // current + ~1 each side + a little slack
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

  // Swap the media only when the previewed item changes — never on a keep/remove
  // toggle (so a playing video isn't interrupted).
  if (pvImg.dataset.uuid === uuid) return;
  pvImg.dataset.uuid = uuid;
  if (p.is_video) {
    // real playback with native controls; the full-res frame is the poster
    pvImg.style.backgroundImage = "";
    pvImg.innerHTML = `<video controls playsinline preload="metadata" poster="${escapeHtml(p.thumb)}?px=${PREVIEW_PX}" src="/api/video/${encodeURIComponent(uuid)}"></video>`;
  } else {
    // reuse the cached full-res image element (instant + sharp if pre-loaded as a
    // neighbour); the grid thumb is a soft placeholder for the rare cold case.
    pvImg.style.backgroundImage = `url("${p.thumb}")`;
    const img = pvGet(uuid);
    pvImg.innerHTML = "";
    pvImg.appendChild(img);
    if (img.complete && img.naturalWidth) img.classList.add("ready");        // already loaded → instant
    else { img.classList.remove("ready"); img.addEventListener("load", () => { if (pvImg.dataset.uuid === uuid) img.classList.add("ready"); }, { once: true }); }
  }

  // Pre-load ±1 in each direction (full-res) once settled, so the next move is seamless.
  clearTimeout(pvWarmTimer);
  pvWarmTimer = setTimeout(() => { if (state.selUuid === uuid) warmNeighbors(1); }, 120);
}
/* Neighbour walking. Cards are direct children of a .gbody, and groups/sections
   stack top-to-bottom, so DOM order == reading order — stepping via siblings is
   O(1)-ish instead of materialising a 50k-card array on every keypress. */
function nextGroupEl(gEl) {
  let n = gEl.nextElementSibling;
  while (n && !n.classList.contains("group")) n = n.nextElementSibling;
  if (n) return n;
  let sec = gEl.closest(".section");
  sec = sec && sec.nextElementSibling;
  while (sec) {
    const g = sec.querySelector && sec.querySelector(".group");
    if (g) return g;
    sec = sec.nextElementSibling;
  }
  return null;
}
function prevGroupEl(gEl) {
  let n = gEl.previousElementSibling;
  while (n && !n.classList.contains("group")) n = n.previousElementSibling;
  if (n) return n;
  let sec = gEl.closest(".section");
  sec = sec && sec.previousElementSibling;
  while (sec) {
    const gs = sec.querySelectorAll ? sec.querySelectorAll(".group") : [];
    if (gs.length) return gs[gs.length - 1];
    sec = sec.previousElementSibling;
  }
  return null;
}
function nextCardFrom(card) {
  if (card.nextElementSibling) return card.nextElementSibling;
  let gEl = card.closest(".group");
  while (gEl) {
    gEl = nextGroupEl(gEl);
    if (gEl && !gEl.classList.contains("collapsed")) {
      const c = gEl.querySelector(".card[data-uuid]");
      if (c) return c;
    }
  }
  return null;
}
function prevCardFrom(card) {
  if (card.previousElementSibling) return card.previousElementSibling;
  let gEl = card.closest(".group");
  while (gEl) {
    gEl = prevGroupEl(gEl);
    if (gEl && !gEl.classList.contains("collapsed")) {
      const cs = gEl.querySelectorAll(".card[data-uuid]");
      if (cs.length) return cs[cs.length - 1];
    }
  }
  return null;
}
function warmNeighbors(span) {
  const cur = selectedCardEl(); if (!cur) return;
  let a = cur, b = cur;
  for (let d = 1; d <= span; d++) {
    a = a && prevCardFrom(a); b = b && nextCardFrom(b);
    if (a) pvGet(a.dataset.uuid);    // loads full-res into the element cache + server RAM cache
    if (b) pvGet(b.dataset.uuid);
  }
}
function moveSelection(key) {
  const cur = selectedCardEl();
  if (!cur || cur.closest(".group").classList.contains("collapsed")) {
    const first = app.querySelector(".group:not(.collapsed) .card[data-uuid]");
    if (first) selectCard(first);
    return;
  }
  let next = key === "ArrowRight" ? nextCardFrom(cur) : prevCardFrom(cur);
  if (!next && key === "ArrowRight" && chunkHasWork()) {
    renderChunk();                                    // walked past the rendered edge
    next = nextCardFrom(cur);
  }
  if (next) selectCard(next);
}
// Up/Down move by a visual row. Rows top-align (align-items:flex-start), so cards
// in a row share ~the same `top`. Walk neighbours outward from the current card and
// measure only until the target row ends — never every rendered card.
function moveSelectionVert(dir, retried) {
  const cur = selectedCardEl();
  if (!cur || cur.closest(".group").classList.contains("collapsed")) return;
  const cr = cur.getBoundingClientRect(), midX = cr.left + cr.width / 2, tol = 4;
  const step = dir === "down" ? nextCardFrom : prevCardFrom;
  const past = dir === "down" ? (t) => t > cr.top + tol : (t) => t < cr.top - tol;
  let c = step(cur), rowTop = null, best = null, bestDx = Infinity;
  while (c) {
    const r = c.getBoundingClientRect();
    if (past(r.top)) {
      if (rowTop === null) rowTop = r.top;             // first card of the adjacent row
      if (Math.abs(r.top - rowTop) > tol) break;       // row after the target — done
      const dx = Math.abs(r.left + r.width / 2 - midX);
      if (dx < bestDx) { bestDx = dx; best = c; }
    }
    c = step(c);
  }
  if (!best && dir === "down" && !retried && chunkHasWork()) {
    renderChunk();                                     // the next row wasn't rendered yet
    return moveSelectionVert(dir, true);
  }
  if (best) selectCard(best);
}
function toggleSelected() {
  const card = selectedCardEl();
  if (card) { flip(card); fillPreview(); }
}

function updateBars() {
  // light-touch refresh of the counters without rebuilding the grid
  const c = counts();
  const pct = c.items ? Math.round(((c.keep + c.rem) / c.items) * 100) : 0;
  const layers = reviewLayers();
  const shown = state.manual ? Math.max(c.items, state.allTotal || 0) : c.items;
  const lbl = $("#itemsLbl");
  if (lbl) lbl.textContent = `${fmtN(shown)} items in ${layers.length} categor${layers.length === 1 ? "y" : "ies"}`;
  app.querySelectorAll(".keep-n").forEach((e) => e.textContent = `Keeping ${fmtN(c.keep)}`);
  app.querySelectorAll(".rem-n").forEach((e) => e.textContent = `Removing ${fmtN(c.rem)}`);
  app.querySelectorAll(".frees-n").forEach((e) => e.textContent = fmtSave(c.bytes));
  const mp = $(".mini-prog > span"); if (mp) mp.style.width = pct + "%";
  const fin = $("#finalize"); if (fin) fin.disabled = !c.rem;
  scheduleSaveReview();   // mirror decision changes so a quit/crash loses (almost) nothing
}

// Persisting 50k decisions stringifies megabytes — coalesce bursts of clicks into
// one write shortly after the last change (plus a pagehide flush at the bottom).
let saveTimer = null;
function scheduleSaveReview() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => { saveTimer = null; saveReviewState(); }, 250);
}

/* ---- finalize ------------------------------------------------------------- */
function openFinalize() {
  if (saveTimer) { clearTimeout(saveTimer); saveTimer = null; saveReviewState(); }  // flush pending save
  state.finalize = "confirm";
  syncModal();
}

function modalHtml() {
  const c = counts();
  if (state.finalize === "confirm") {
    return `<div class="backdrop"><div class="modal">
      <h3>Review &amp; Finalize</h3>
      ${state.finalizeErr ? `<p class="head-n" style="color:var(--pc-warn)">Finalize failed: ${escapeHtml(state.finalizeErr)} — your decisions are intact, try again.</p>` : ""}
      <p class="head-n">Keeping ${fmtN(c.keep)} · removing ${fmtN(c.rem)} items · frees ${fmtSave(c.bytes)}</p>
      <div class="rows">
        <div class="row">${tick()}<span>macOS will ask you to confirm before anything is removed.</span></div>
        <div class="row">${tick()}<span>Removed items go to Recently Deleted — recoverable for 30 days.</span></div>
        <div class="row">${tick()}<span>${state.manual
          ? "Kept items are left untouched. Nothing leaves your Mac."
          : "Kept items are marked reviewed and won't be shown again. Nothing leaves your Mac."}</span></div>
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
  syncModal();
  try {
    let toDelete;
    if (state.manual) {
      // Manual feed: just remove the marked items — don't record the whole library
      // as reviewed (that's only for the curated curator-training flow). Decisions
      // include resumed verdicts from not-yet-fetched pages; a uuid that's gone
      // from the library comes back "unmatched" and is reported on the done screen.
      toDelete = Object.entries(state.decisions.all || {}).filter(([, v]) => v !== "keep").map(([u]) => u);
    } else {
      const layers = Object.keys(state.decisions);
      for (const layer of layers) {
        const idx = state.idx[layer];      // O(1) lookups instead of a scan per uuid
        const decisions = Object.entries(state.decisions[layer]).map(([uuid, v]) => {
          const ent = idx && idx.get(uuid);
          return { uuid, verdict: v === "keep" ? "keep" : "discard",
            group_key: ent ? ent.gkey : null, suggested: ent ? ent.p.suggested_keep : false };
        });
        await api.post("/api/decisions", { layer, decisions });
      }
      toDelete = (await api.post("/api/finalize", { layers })).to_delete;
    }
    const del = await api.post("/api/delete", { uuids: toDelete });
    state.done = { status: del.status, deleted: del.deleted || 0,
                   kept: snapshot.keep, bytes: snapshot.bytes,
                   unmatched: (del.unmatched || []).length };
    state.finalize = "done";
    if (del.status === "ok" || del.status === "no-match") clearReviewState();
    syncModal();
  } catch (e) {
    // Back to the confirm modal with the error inline — decisions are intact,
    // "Remove N" doubles as the retry.
    state.finalizeErr = e.message;
    state.finalize = "confirm";
    syncModal();
  }
}

function doneHtml() {
  const d = state.done || {};
  const doneCta = state.manual ? "Back to categories" : "Start a new review";
  if (d.status === "unauthorized") {
    return `<div class="backdrop"><div class="modal center">
      <div class="done-disc" style="background:var(--pc-warn)">${icon("i-lock")}</div>
      <h3>Photos access needed</h3>
      <p class="head-n">To remove items, allow Library Cleanup in System Settings ▸ Privacy &amp;
        Security ▸ <b>Photos</b>, then try again.</p>
      <p class="head-n" style="font-size:12px">Your review is kept — nothing was changed.</p>
      <div class="actions" style="justify-content:center"><button class="btn btn-primary" id="m-back">Back to review</button></div>
    </div></div>`;
  }
  if (d.status === "access-limited") {
    return `<div class="backdrop"><div class="modal center">
      <div class="done-disc" style="background:var(--pc-warn)">${icon("i-lock")}</div>
      <h3>Full Photos access needed</h3>
      <p class="head-n">${d.deleted ? `Removed ${fmtN(d.deleted)}, but ${fmtN(d.unmatched)} item${d.unmatched === 1 ? "" : "s"} couldn't be reached` : "Library Cleanup can't reach the selected items"} because Photos access is set to <b>Selected Photos</b>. Switch it to <b>All Photos</b> in System Settings ▸ Privacy &amp; Security ▸ <b>Photos</b>, then try again.</p>
      <p class="head-n" style="font-size:12px">Your review is kept — finish removing once full access is granted.</p>
      <div class="actions" style="justify-content:center"><button class="btn btn-primary" id="m-back">Back to review</button></div>
    </div></div>`;
  }
  if (d.status && d.status !== "ok") {
    return `<div class="backdrop"><div class="modal center">
      <div class="done-disc" style="background:var(--pc-warn)">${icon("i-x")}</div>
      <h3>Nothing was removed</h3>
      <p class="head-n">${d.status === "error" ? "Removal was cancelled." : "No matching items were found."}
        Your keepers are marked reviewed.</p>
      <div class="actions" style="justify-content:center"><button class="btn btn-primary" id="m-new">${doneCta}</button></div>
    </div></div>`;
  }
  return `<div class="backdrop"><div class="modal center">
    <div class="done-disc">${icon("i-check")}</div>
    <h3>All done</h3>
    <p class="head-n">Removed ${fmtN(d.deleted)} · kept ${fmtN(d.kept)} · freed up to ${fmtSave(d.bytes)}.</p>
    ${d.unmatched ? `<p class="head-n" style="font-size:12px;color:var(--pc-warn)">${fmtN(d.unmatched)} item${d.unmatched === 1 ? " was" : "s were"} no longer in the library and ${d.unmatched === 1 ? "was" : "were"} skipped.</p>` : ""}
    <p class="head-n" style="font-size:12px">Removed items stay in Recently Deleted for 30 days.</p>
    <div class="actions" style="justify-content:center"><button class="btn btn-primary" id="m-new">${doneCta}</button></div>
    <div class="support-card">
      <div class="support-msg">Library Cleanup is free and runs entirely on your Mac. If it saved you some time, you can buy me a coffee.</div>
      <button class="btn support-btn" id="supportDone">${icon("i-coffee")} Buy me a coffee</button>
    </div>
  </div></div>`;
}

/* ---- preview size (slider + ⌘-scroll) ------------------------------------- */
function setCardSize(px) {
  px = Math.max(84, Math.min(240, Math.round(px)));
  state.cardSize = px;
  document.documentElement.style.setProperty("--card", px + "px");
  const cs = $("#cardsize");
  if (cs && +cs.value !== px) cs.value = px;
}

const PV_MIN = 300, PV_MAX = 640;         // preview panel: min keeps content uncramped
// Responsive default: on a wide window the preview should be generously wide
// (a photo is the point), but always leave the grid a comfortable minimum.
function defaultPreviewWidth() {
  const w = window.innerWidth || 1280;
  return Math.round(Math.max(PV_MIN, Math.min(PV_MAX, w * 0.34, w - 560)));
}
function setPreviewWidth(px) {
  state.pvWidth = Math.max(PV_MIN, Math.round(px));
  document.documentElement.style.setProperty("--pv", state.pvWidth + "px");
}

/* ---- boot ----------------------------------------------------------------- */
// Nothing here touches the photo library — the first library access (and any
// Photos permission prompt) happens only when the user clicks "Analyze".
setCardSize(state.cardSize);
setPreviewWidth(defaultPreviewWidth());
// Keep the preview at its responsive default as the window resizes — until the
// user drags the divider, after which their chosen width sticks.
window.addEventListener("resize", () => {
  if (!state.pvUserSized && state.view === "review" && !state.pvCollapsed) {
    setPreviewWidth(defaultPreviewWidth());
  }
});
// Flush a pending (debounced) review save if the WebView goes away mid-burst.
window.addEventListener("pagehide", () => {
  if (saveTimer) { clearTimeout(saveTimer); saveTimer = null; saveReviewState(); }
});
// health is library-free (reads only the app's own SQLite) — safe at boot;
// used to show the version in the footer.
api.get("/api/health").then((h) => { state.version = h.version || ""; render(); }).catch(() => {});
// Check GitHub for a newer release (anonymous; no library data sent). Only prompts
// on the home screen so an in-progress review is never interrupted; "Later"
// dismisses until the next launch.
api.get("/api/update/check").then((u) => {
  if (u && u.available && state.view === "home") { state.update = u; state.updateStatus = "prompt"; render(); }
}).catch(() => {});
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
  else if (e.key === "ArrowUp" || e.key === "ArrowDown") { e.preventDefault(); moveSelectionVert(e.key === "ArrowDown" ? "down" : "up"); }
  else if (e.key === " " || e.key === "Enter") { e.preventDefault(); toggleSelected(); }   // the current selection
});
render();
