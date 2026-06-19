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
const fmtGB = (b) => `${((b || 0) / 1073741824).toFixed(1)} GB`;
const icon = (id, cls = "") => `<svg class="${cls}"><use href="#${id}"/></svg>`;

const CATS = [
  { id: "dedup", name: "Duplicate photoshoots", grouped: true, setword: "bursts", noun: "photos",
    desc: "Fired the shutter 50 times at the same spot? No problem." },
  { id: "screenshots", name: "Screenshots", grouped: false, noun: "screenshots",
    desc: "Work pings you screenshotted and never reopened." },
  { id: "videos", name: "Duplicate videos", grouped: true, setword: "sets", noun: "videos",
    desc: "Ten takes of the same wave? We keep the steady one." },
  { id: "expired", name: "Expired utility photos", grouped: false, noun: "photos",
    desc: "That parking-spot photo from a garage you left in 2022." },
];
const CAT = Object.fromEntries(CATS.map((c) => [c.id, c]));

const state = {
  view: "home",
  phase: "idle",            // idle | scanning | results
  lib: null,                // {photos, videos}
  summary: null,            // {layer: {groups, items, removable, reclaimable_bytes}}
  selected: new Set(),
  candidates: {},           // layer -> groups
  decisions: {},            // layer -> {uuid: 'keep'|'remove'}
  collapsed: new Set(),
  finalize: null,           // null | 'confirm' | 'working' | 'done'
  done: null,
};

let scanAbort = null;

/* ---- chrome --------------------------------------------------------------- */
function chrome(inner) {
  const lib = state.lib;
  const status = lib
    ? `Library connected · ${fmtN(lib.photos)} photos · ${fmtN(lib.videos)} videos`
    : "Library connected";
  return `
    <div class="chrome">
      <div class="topbar">
        <div class="brand"><svg viewBox="0 0 1024 1024"><use href="#appicon"/></svg> Library Cleanup</div>
        <div class="status"><span class="dot"></span>${status}</div>
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
    const past = state.summary
      ? "" : `<div class="past">Everything stays on your Mac.</div>`;
    body = `
      <div class="scroll"><div class="home"><div class="hero">
        <svg class="appicon" viewBox="0 0 1024 1024"><use href="#appicon"/></svg>
        <h1>Tidy your photo library</h1>
        <p class="sub">We scan on your Mac, pre-pick the best of every burst and flag clutter.
          You just review the suggestions and confirm.</p>
        <button class="btn btn-primary" id="analyze">Analyze my library</button>
        ${past}
      </div></div></div>
      <div class="foot-note">${icon("i-lock")} Everything runs on your Mac. Nothing is uploaded, ever.</div>`;
  } else if (state.phase === "scanning") {
    body = `
      <div class="scroll"><div class="home"><div class="scanning">
        <div class="spinner"></div>
        <h2>Analyzing your library…</h2>
        <div class="step" id="step">Reading your library…</div>
        <div class="progress"><span id="bar"></span></div>
        <div style="margin-top:22px"><button class="btn-text" id="cancel">Cancel</button></div>
      </div></div></div>`;
  } else {
    body = `<div class="scroll"><div class="home"><div class="results">
        <h2>Here's what we found</h2>
        <div class="sub">Pick what to review. We've pre-selected the best of each group to keep.</div>
        <div class="cat-list">${CATS.map(catCard).join("")}</div>
      </div></div></div>
      ${resultsBar()}`;
  }
  app.innerHTML = chrome(body);
  bindHome();
}

function catCard(c) {
  const s = state.summary && state.summary[c.id];
  const has = s && s.items > 0;
  const on = state.selected.has(c.id);
  const sub = c.grouped
    ? `across ${fmtN(s ? s.groups : 0)} ${c.setword}`
    : "flagged to remove";
  const count = has ? `${fmtN(s.items)} <span style="font-weight:400;color:var(--pc-text-tertiary)">${c.noun}</span>` : "—";
  return `
    <button class="cat ${on ? "on" : ""} ${has ? "" : "disabled"}" data-cat="${c.id}" ${has ? "" : "disabled"}>
      <span class="check">${icon("i-check")}</span>
      <span class="body"><div class="name">${c.name}</div><div class="desc">${c.desc}</div></span>
      <span class="right">
        <div class="count">${count}</div>
        <div class="${has ? "save" : ""}">${has ? "Save up to " + fmtGB(s.reclaimable_bytes) : ""}</div>
        ${has ? `<div class="desc">${sub}</div>` : `<span class="soon">None found</span>`}
      </span>
    </button>`;
}

function resultsBar() {
  const sel = [...state.selected];
  const gb = sel.reduce((a, id) => a + (state.summary[id]?.reclaimable_bytes || 0), 0);
  return `<div class="bar bottom">
      <button class="btn-text" id="rescan">Re-scan</button>
      <div style="flex:1"></div>
      <div style="color:var(--pc-text-tertiary)">${sel.length} categor${sel.length === 1 ? "y" : "ies"} · save up to ${fmtGB(gb)}</div>
      <button class="btn btn-primary" id="review" ${sel.length ? "" : "disabled"}>Review ${sel.length} categor${sel.length === 1 ? "y" : "ies"}</button>
    </div>`;
}

function bindHome() {
  const a = $("#analyze"); if (a) a.onclick = startAnalyze;
  const c = $("#cancel"); if (c) c.onclick = () => { if (scanAbort) scanAbort.abort(); };
  const rs = $("#rescan"); if (rs) rs.onclick = () => { state.phase = "idle"; render(); };
  const rv = $("#review"); if (rv) rv.onclick = enterReview;
  app.querySelectorAll(".cat[data-cat]").forEach((btn) => {
    btn.onclick = () => {
      const id = btn.dataset.cat;
      state.selected.has(id) ? state.selected.delete(id) : state.selected.add(id);
      render();
    };
  });
}

async function startAnalyze() {
  state.phase = "scanning";
  render();
  scanAbort = new AbortController();
  const steps = ["Reading your library…", "Computing on-device similarity…",
    "Grouping photoshoots…", "Scanning for screenshots & clutter…", "Almost done…"];
  let i = 0, pct = 6;
  const stepEl = $("#step"), barEl = $("#bar");
  if (barEl) barEl.style.width = pct + "%";
  const timer = setInterval(() => {
    i = Math.min(i + 1, steps.length - 1);
    pct = Math.min(pct + 16, 90);
    if (stepEl) stepEl.textContent = steps[i];
    if (barEl) barEl.style.width = pct + "%";
  }, 1100);
  try {
    const res = await api.post("/api/analyze", { layers: CATS.map((c) => c.id) },
      { signal: scanAbort.signal });
    clearInterval(timer);
    state.summary = res.summary;
    state.selected = new Set(CATS.filter((c) => res.summary[c.id]?.items > 0).map((c) => c.id));
    // library totals for the status line — fetched now (post-scan), not at launch
    api.get("/api/library-stats").then((s) => { state.lib = s; render(); }).catch(() => {});
    if (barEl) barEl.style.width = "100%";
    setTimeout(() => { state.phase = "results"; render(); }, 250);
  } catch (e) {
    clearInterval(timer);
    if (e.name === "AbortError") { state.phase = "idle"; render(); }
    else { state.phase = "idle"; render(); alert("Analyze failed: " + e.message); }
  }
}

/* ---- review --------------------------------------------------------------- */
async function enterReview() {
  state.view = "review";
  state.candidates = {};
  state.decisions = {};
  app.innerHTML = chrome(`<div class="scroll"><div class="review"><div class="scanning">
      <div class="spinner"></div><h2>Loading review…</h2></div></div></div>`);
  const layers = CATS.map((c) => c.id).filter((id) => state.selected.has(id));
  for (const layer of layers) {
    const res = await api.get(`/api/candidates?layer=${layer}`);
    state.candidates[layer] = res.groups;
    const d = {};
    res.groups.forEach((g) => g.photos.forEach((p) => {
      d[p.uuid] = p.decided ? (p.decided === "keep" ? "keep" : "remove")
        : (p.suggested_keep ? "keep" : "remove");
    }));
    state.decisions[layer] = d;
  }
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
    </div>
    <div class="scroll"><div class="review">${sections}</div></div>
    <div class="bar bottom">
      <button class="btn-text" id="back">‹ Back</button>
      <div style="flex:1"></div>
      <div style="color:var(--pc-text-tertiary)">
        <span class="keep-n">Keeping ${fmtN(c.keep)}</span> ·
        <span class="rem-n">Removing ${fmtN(c.rem)}</span> · Frees ${fmtGB(c.bytes)}</div>
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
  return `<div class="section"><h3>${c.name}</h3><div class="summary">${summary}</div>${blocks}</div>`;
}

function groupHtml(layer, g) {
  const noun = layer === "videos" ? "clips" : "shots";
  const d = state.decisions[layer];
  const keep = g.photos.filter((p) => d[p.uuid] === "keep").length;
  const rem = g.photos.length - keep;
  const collapsed = state.collapsed.has(g.group_key);
  return `<div class="group ${collapsed ? "collapsed" : ""}" data-group="${g.group_key}">
    <div class="ghead">
      <span class="gtitle">${g.title}</span>
      <span class="gmeta">· ${g.size} ${noun} · keep ${keep} · remove ${rem}</span>
      <span class="spacer"></span>
      <button class="btn-text" data-all="keep" data-layer="${layer}" data-g="${g.group_key}">Keep all</button>
      <button class="btn-text" data-all="remove" data-layer="${layer}" data-g="${g.group_key}">Remove all</button>
      <button class="chev" data-collapse="${g.group_key}">${collapsed ? "▸" : "▾"}</button>
    </div>
    <div class="gbody">${g.photos.map((p) => cardHtml(layer, p)).join("")}</div>
  </div>`;
}

function flatHtml(layer, g) {
  return `<div class="group"><div class="ghead">
      <span class="gtitle">All flagged to remove</span><span class="gmeta">· tap any to keep</span>
      <span class="spacer"></span>
      <button class="btn-text" data-all="keep" data-layer="${layer}" data-g="${g.group_key}">Keep all</button>
      <button class="btn-text" data-all="remove" data-layer="${layer}" data-g="${g.group_key}">Remove all</button>
    </div><div class="gbody">${g.photos.map((p) => cardHtml(layer, p, true)).join("")}</div></div>`;
}

function cardHtml(layer, p, shot = false) {
  const v = state.decisions[layer][p.uuid];
  const badge = v === "keep" ? `<span class="badge">${icon("i-check")}</span>`
    : `<span class="badge">${icon("i-x")}</span>`;
  const fav = p.favorite ? `<svg class="fav"><use href="#i-heart"/></svg>` : "";
  let overlay = "";
  if (p.is_video) {
    const dur = p.duration ? `${Math.floor(p.duration / 60)}:${String(Math.round(p.duration % 60)).padStart(2, "0")}` : "";
    overlay = `<div class="vplay">${icon("i-play")}</div>${dur ? `<span class="vdur">${dur}</span>` : ""}`;
  }
  const meta = p.is_video
    ? `${p.width}×${p.height}` : `score ${p.score} · ${p.width}×${p.height}`;
  const cap = shot && p.subtitle ? `<div class="cap">${escapeHtml(p.subtitle)}</div>`
    : `<div class="fn">${escapeHtml(p.filename)}</div>`;
  return `<div class="card ${shot ? "shot" : ""} ${v === "keep" ? "keep" : "remove"}" data-uuid="${p.uuid}" data-layer="${layer}">
    <div class="frame" tabindex="0" role="button" aria-pressed="${v === "keep"}">
      <img src="${p.thumb}" loading="lazy" alt="">
      ${fav}${overlay}${badge}
      <div class="meta">${meta}</div>
    </div>${cap}</div>`;
}

function escapeHtml(s) {
  return (s || "").replace(/[&<>"]/g, (m) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[m]));
}

function bindReview() {
  $("#back").onclick = () => { state.view = "home"; state.phase = "results"; render(); };
  const fin = $("#finalize"); if (fin) fin.onclick = openFinalize;

  app.querySelectorAll(".card[data-uuid]").forEach((card) => {
    const toggle = () => flip(card);
    $(".frame", card).onclick = toggle;
    $(".frame", card).onkeydown = (e) => {
      if (e.key === " " || e.key === "Enter") { e.preventDefault(); toggle(); }
      else if (e.key.startsWith("Arrow")) moveFocus(card, e.key);
    };
  });
  app.querySelectorAll("[data-collapse]").forEach((b) => b.onclick = () => {
    const k = b.dataset.collapse;
    state.collapsed.has(k) ? state.collapsed.delete(k) : state.collapsed.add(k);
    renderReview();
  });
  app.querySelectorAll("[data-all]").forEach((b) => b.onclick = () => {
    const { layer, g, all } = b.dataset;
    const grp = (state.candidates[layer] || []).find((x) => x.group_key === g);
    grp.photos.forEach((p) => (state.decisions[layer][p.uuid] = all));
    renderReview();
  });

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

function flip(card) {
  const { uuid, layer } = card.dataset;
  const v = state.decisions[layer][uuid] === "keep" ? "remove" : "keep";
  state.decisions[layer][uuid] = v;
  card.classList.toggle("keep", v === "keep");
  card.classList.toggle("remove", v !== "keep");
  $(".badge", card).innerHTML = icon(v === "keep" ? "i-check" : "i-x");
  $(".frame", card).setAttribute("aria-pressed", v === "keep");
  updateBars();
}

function moveFocus(card, key) {
  const cards = [...app.querySelectorAll(".card[data-uuid] .frame")];
  const i = cards.indexOf($(".frame", card));
  const next = key === "ArrowRight" || key === "ArrowDown" ? i + 1 : i - 1;
  if (cards[next]) cards[next].focus();
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
      <p class="head-n">Keeping ${fmtN(c.keep)} · removing ${fmtN(c.rem)} items · frees ${fmtGB(c.bytes)}</p>
      <div class="rows">
        <div class="row">${tick()}<span>macOS will ask you to confirm before anything is removed.</span></div>
        <div class="row">${tick()}<span>Removed items go to Recently Deleted — recoverable for 30 days.</span></div>
        <div class="row">${tick()}<span>Kept items are marked reviewed and won't be shown again. Nothing leaves your Mac.</span></div>
      </div>
      <div class="actions">
        <button class="btn" id="m-cancel">Go back</button>
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
    <p class="head-n">Removed ${fmtN(d.deleted)} · kept ${fmtN(d.kept)} · freed up to ${fmtGB(d.bytes)}.</p>
    <p class="head-n" style="font-size:12px">Removed items stay in Recently Deleted for 30 days.</p>
    <div class="actions" style="justify-content:center"><button class="btn btn-primary" id="m-new">Start a new review</button></div>
  </div></div>`;
}

function findGroupKey(layer, uuid) {
  const g = (state.candidates[layer] || []).find((x) => x.photos.some((p) => p.uuid === uuid));
  return g ? g.group_key : null;
}

/* ---- boot ----------------------------------------------------------------- */
// Nothing here touches the photo library — the first library access (and any
// Photos permission prompt) happens only when the user clicks "Analyze".
render();
