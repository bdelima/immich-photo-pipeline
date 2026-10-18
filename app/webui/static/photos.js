// The Photos view of the main page (#photos): a read-only, scrolling timeline of everyone's photos
// (those who partnered the pipeline account in Immich), with a full-window viewer. Pictures come
// through the pipeline's own proxy routes, never straight from Immich.
//
// Layout: the photos scroll with their own scroll bar; the timeline (year > month > day) is a narrow
// column to the right of that bar. Scrolling the photos moves the timeline's highlight; clicking a
// row brings the newest photos in it to the top.
//
// Speed: every month's list is kept for the session once fetched, and only the rows near the screen
// have tiles in the page. The tiles are grey boxes of the right size that fade the picture in, so
// nothing jumps while pictures arrive. The next and previous months are fetched ahead.
const PhotosPane = (() => {
  const ROW = 200, GAP = 4, DAYH = 30;
  const DATA_NEAR = 2400;        // a month this close to the screen has its list fetched
  const TILE_NEAR = 900;         // rows this close to the screen have their tiles in the page
  const FOLLOW_MS = 120;         // the timeline waits for scrolling to pause this long
  const WARM = 18;               // thumbnails of a neighbouring month fetched ahead
  const MAX_LOADS = 4;           // month lists being fetched at once
  const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
  const HELP = "Each household member needs to add the pipeline account as a partner in Immich " +
    "(Account settings &rarr; Sharing &rarr; Partners). Their photos then appear here.";

  const pane = $("photosView");
  let built = false, started = false, sc, tl, host, note;
  const secs = {};            // bucket -> { bucket, month, count, el, head, body, items, layout, rendered, ... }
  let order = [];             // buckets, newest first
  let inflight = 0, savedTop = 0;

  const monthLabel = m => { const [y, mo] = m.split("-"); return MONTHS[+mo - 1] + " " + y; };
  const shortMonth = m => MONTHS[+m.slice(5, 7) - 1].slice(0, 3);
  const dayLabel = d => new Date(d + "T12:00:00").toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric", year: "numeric" });
  const dayShort = d => String(+d.slice(8, 10));        // the timeline column is narrow: the day number only
  const dur = s => { s = Math.round(s); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), r = s % 60;
    return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(r).padStart(2, "0"); };
  const niceBytes = n => n >= 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1024)) + " KB";
  const thumbUrl = id => "/api/household/" + id + "/thumb";
  const width = () => Math.max(200, host.clientWidth);

  function showNote(html) { note.innerHTML = html; note.hidden = !html; }

  // ---- building the view ----
  function build() {
    built = true;
    pane.innerHTML =
      '<div class="ph-scroll" id="ph-scroll"><div class="card head"><h2>Photos</h2><span id="ph-count" class="chip"></span></div>' +
      '<div id="ph-note" class="ph-note" hidden></div><div id="ph-months"></div></div>' +
      '<div class="ph-tl" id="ph-tl" role="tree" aria-label="Timeline"></div>';
    sc = $("ph-scroll"); tl = $("ph-tl"); host = $("ph-months"); note = $("ph-note");
    sc.addEventListener("scroll", () => { schedule(); clearTimeout(followTimer); followTimer = setTimeout(follow, FOLLOW_MS); }, { passive: true });
    let rz = null;
    window.addEventListener("resize", () => { if (pane.hidden) return; clearTimeout(rz); rz = setTimeout(relayoutAll, 150); });
  }

  async function start() {
    started = true;
    let data;
    try { data = await api("/api/household/months"); }
    catch (e) { started = false; showNote("<b>Couldn't read the timeline from Immich.</b> " + esc(e.message || String(e))); return; }
    showNote("");
    order = data.months.map(m => m.bucket);
    const total = data.months.reduce((n, m) => n + m.count, 0);
    $("ph-count").textContent = total ? "about " + total.toLocaleString() + " photos" : "";
    if (!order.length) { showNote("<b>Nothing to show yet.</b> " + HELP); return; }
    const W = width();
    for (const m of data.months) {
      const el = document.createElement("section");
      el.className = "ph-month";
      el.innerHTML = '<h3 class="ph-mh">' + esc(monthLabel(m.month)) + ' <span class="muted">' + m.count.toLocaleString() + '</span></h3><div class="ph-body"></div>';
      const s = { bucket: m.bucket, month: m.month, count: m.count, el, head: el.firstChild, body: el.lastChild, items: null,
        layout: null, rendered: new Map(), state: "idle", promise: null, failed: false, warmed: false };
      s.body.style.height = estimate(s, W) + "px";
      host.appendChild(el);
      secs[m.bucket] = s;
    }
    buildTimeline(data.months);
    schedule();
    follow();
  }

  // The height of a month whose photos are not known yet: a guess from its count, replaced by the
  // real height once its list arrives.
  function estimate(s, W) {
    const perRow = Math.max(1, Math.floor(W / (ROW * 1.4)));
    return Math.ceil(s.count / perRow) * (ROW + GAP) + Math.min(s.count, 12) * DAYH + 8;
  }

  // ---- fetching a month and laying it out ----
  function ensure(s, force) {
    if (s.items) return Promise.resolve(s);
    if (s.promise) return s.promise;
    if (s.failed || (!force && inflight >= MAX_LOADS)) return Promise.resolve(s);
    inflight++; s.state = "loading";
    s.promise = api("/api/household/month?bucket=" + encodeURIComponent(s.bucket))
      .then(d => { s.items = d.items; s.state = "ready"; layoutSection(s); return s; })
      .catch(e => { s.failed = true; s.state = "idle"; failedMonth(s, e); return s; })
      .finally(() => { inflight--; s.promise = null; schedule(); });
    return s.promise;
  }

  function failedMonth(s, e) {
    s.body.innerHTML = '<p class="muted ph-fail">Couldn\'t load this month. ' + esc(e && e.message || "") + ' <button class="btn sec sm">Try again</button></p>';
    s.body.lastChild.querySelector("button").onclick = () => { s.failed = false; s.body.innerHTML = ""; ensure(s, true); };
  }

  // The rows of a month: a header per day, then justified rows of tiles. All sizes are known up
  // front, so the month has its true height before a single tile is drawn.
  function computeLayout(s, W) {
    const rows = [], dayTop = {}, days = [];
    let y = 0;
    for (const it of s.items) { const d = days[days.length - 1]; if (d && d.day === it.day) d.items.push(it); else days.push({ day: it.day, items: [it] }); }
    for (const d of days) {
      dayTop[d.day] = y; rows.push({ kind: "day", top: y, h: DAYH, label: dayLabel(d.day) }); y += DAYH;
      let row = [], sum = 0;
      const flush = last => {
        if (!row.length) return;
        const h = last ? ROW : Math.min((W - GAP * (row.length - 1)) / sum, ROW * 1.6);
        rows.push({ kind: "row", top: y, h, tiles: row.map(it => ({ it, w: h * it.ratio })) });
        y += h + GAP; row = []; sum = 0;
      };
      for (const it of d.items) {
        row.push(it); sum += it.ratio;
        if ((W - GAP * (row.length - 1)) / sum <= ROW) flush(false);
      }
      flush(true);
    }
    s.layout = { W, rows, dayTop, days: days.map(d => d.day), h: Math.max(80, y + 8) };
  }

  // A month that is entirely above the screen can change height without moving what is on the
  // screen only if the scroll position moves with it.
  function setHeight(s, h) {
    const was = s.body.offsetHeight;
    if (Math.abs(h - was) < 1) return;
    const above = s.el.getBoundingClientRect().bottom <= sc.getBoundingClientRect().top;
    s.body.style.height = h + "px";
    if (above) sc.scrollTop += h - was;
  }

  function layoutSection(s) {
    clearRows(s);
    s.body.innerHTML = "";
    if (!s.items.length) { s.layout = { W: width(), rows: [], dayTop: {}, days: [], h: 80 }; s.body.innerHTML = '<p class="muted">Nothing here.</p>'; setHeight(s, 80); return; }
    computeLayout(s, width());
    setHeight(s, s.layout.h);
    fillDays(s);
  }

  function relayoutAll() {
    const W = width();
    for (const b of order) {
      const s = secs[b];
      if (s.layout && s.items && s.items.length) { clearRows(s); computeLayout(s, W); setHeight(s, s.layout.h); }
      else if (!s.items) s.body.style.height = estimate(s, W) + "px";
    }
    schedule();
  }

  // ---- drawing only what is near the screen ----
  let raf = 0, followTimer = null;
  function schedule() { if (!raf) raf = requestAnimationFrame(() => { raf = 0; update(); }); }

  function update() {
    if (!built || pane.hidden || !order.length) return;
    const box = sc.getBoundingClientRect();
    for (const b of order) {
      const s = secs[b], r = s.el.getBoundingClientRect();
      if (r.bottom > box.top - DATA_NEAR && r.top < box.bottom + DATA_NEAR && !s.items) ensure(s);
      const near = r.bottom > box.top - TILE_NEAR && r.top < box.bottom + TILE_NEAR;
      if (near && s.items) drawRows(s, box); else if (s.rendered.size) clearRows(s);
    }
  }

  function drawRows(s, box) {
    const lay = s.layout; if (!lay || !lay.rows.length) return;
    const bt = s.body.getBoundingClientRect().top, lo = box.top - TILE_NEAR - bt, hi = box.bottom + TILE_NEAR - bt;
    lay.rows.forEach((row, i) => {
      const visible = row.top + row.h > lo && row.top < hi, have = s.rendered.get(i);
      if (visible && !have) { const el = rowEl(s, row); s.body.appendChild(el); s.rendered.set(i, el); }
      else if (!visible && have) { have.remove(); s.rendered.delete(i); }
    });
  }
  function clearRows(s) { for (const el of s.rendered.values()) el.remove(); s.rendered.clear(); }

  function rowEl(s, row) {
    const el = document.createElement("div");
    el.style.top = row.top + "px";
    if (row.kind === "day") { el.className = "ph-day"; el.textContent = row.label; return el; }
    el.className = "ph-row"; el.style.height = row.h + "px";
    for (const t of row.tiles) el.appendChild(tile(s, t.it, t.w, row.h));
    return el;
  }

  function tile(s, it, w, h) {
    const a = document.createElement("button");
    a.className = "ph-tile"; a.style.width = w + "px"; a.style.height = h + "px";
    a.setAttribute("aria-label", (it.video ? "Video" : "Photo") + (it.owner ? " from " + it.owner : "") + ", " + it.day);
    const img = document.createElement("img"); img.alt = ""; img.decoding = "async";
    img.onload = () => img.classList.add("in");
    img.src = thumbUrl(it.id);
    if (img.complete && img.naturalWidth) img.classList.add("in");
    a.appendChild(img);
    if (it.owner) { const o = document.createElement("span"); o.className = "ph-owner"; o.textContent = it.owner; a.appendChild(o); }
    if (it.video) { const v = document.createElement("span"); v.className = "ph-dur"; v.textContent = "▶ " + dur(it.seconds || 0); a.appendChild(v); }
    a.onclick = () => openViewer(s, s.items.indexOf(it));
    return a;
  }

  // ---- the timeline: year > month > day ----
  const rowOf = {}, kidsOf = {}, isOpen = {}, auto = {};
  let current = null, here = null;

  function node(key, label, count, cls, expandable, parentEl) {
    const w = document.createElement("div"); w.className = cls;
    const r = document.createElement("div"); r.className = "nd"; r.tabIndex = 0; r.setAttribute("role", "treeitem");
    if (expandable) r.setAttribute("aria-expanded", "false");
    r.innerHTML = '<span class="tw' + (expandable ? "" : " leaf") + '">▸</span><span>' + esc(label) + '</span><span class="c">' + (count ? count.toLocaleString() : "") + "</span>";
    w.appendChild(r); rowOf[key] = r;
    r.addEventListener("click", e => { if (expandable && e.target.classList.contains("tw")) setOpen(key, !isOpen[key], true); else jump(key); });
    r.addEventListener("keydown", e => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); jump(key); }
      else if (expandable && e.key === "ArrowRight") { e.preventDefault(); setOpen(key, true, true); }
      else if (expandable && e.key === "ArrowLeft") { e.preventDefault(); setOpen(key, false, true); }
    });
    if (expandable) { const k = document.createElement("div"); k.className = "kids"; k.hidden = true; kidsOf[key] = k; w.appendChild(k); }
    (parentEl || tl).appendChild(w);
    return w;
  }

  function buildTimeline(months) {
    const years = [];
    for (const m of months) {
      const y = m.month.slice(0, 4);
      if (!years.length || years[years.length - 1].y !== y) years.push({ y, n: 0, months: [] });
      const g = years[years.length - 1]; g.n += m.count; g.months.push(m);
    }
    for (const g of years) {
      node(g.y, g.y, g.n, "y", true);                                  // counts are shown on years only
      for (const m of g.months) node(m.month, shortMonth(m.month), 0, "mo", true, kidsOf[g.y]);
    }
  }

  // Days of a month are known once that month's list is: one fetch, then remembered.
  function fillDays(s) {
    const k = kidsOf[s.month]; if (!k || k.dataset.filled) return;
    k.dataset.filled = "1"; k.innerHTML = "";
    for (const d of s.layout.days) node(d, dayShort(d), 0, "dy", false, k);
    if (current && current.startsWith(s.month)) { current = null; follow(); }        // the highlight can now go down to the day
  }

  // `mine` is the person opening or closing a row; what the timeline opens by itself is closed again
  // by itself, but a row the person opened stays as they left it.
  function setOpen(key, on, mine) {
    isOpen[key] = on;
    if (mine) delete auto[key];
    const r = rowOf[key], k = kidsOf[key];
    if (!r || !k) return;
    k.hidden = !on;
    r.querySelector(".tw").textContent = on ? "▾" : "▸";
    r.setAttribute("aria-expanded", on ? "true" : "false");
    const m = on && key.length === 7 ? secs[bucketOfMonth(key)] : null;
    if (m && !m.items) {                                  // its days are not known yet: one fetch, then remembered
      if (!k.dataset.filled) k.innerHTML = '<div class="dy wait">…</div>';
      ensure(m, true);
    }
  }

  const bucketOfMonth = m => order.find(b => secs[b].month === m);

  // The newest photos inside the clicked row go to the top of the screen.
  async function jump(key) {
    const b = key.length === 4 ? order.find(x => secs[x].month.startsWith(key)) : bucketOfMonth(key.slice(0, 7));
    const s = secs[b]; if (!s) return;
    await ensure(s, true);
    if (!s.layout) return;
    const box = sc.getBoundingClientRect();
    if (key.length === 10 && s.layout.dayTop[key] !== undefined) {
      sc.scrollTop += s.body.getBoundingClientRect().top + s.layout.dayTop[key] - box.top - s.head.offsetHeight;
    } else {
      sc.scrollTop += s.el.getBoundingClientRect().top - box.top;
    }
    current = null; schedule(); follow();
  }

  // Which row the photos on screen belong to: the day at the top of the screen once its month is
  // known, otherwise its month. Called when scrolling pauses, so it does not flicker.
  function follow() {
    if (!built || pane.hidden || !order.length) return;
    const box = sc.getBoundingClientRect(), probe = box.top + 64;
    let s = null;
    for (const b of order) { const x = secs[b]; if (x.el.getBoundingClientRect().bottom > probe) { s = x; break; } }
    if (!s) return;
    let want = s.month;
    if (s.layout && s.layout.days.length) {
      const bt = s.body.getBoundingClientRect().top;
      want = s.layout.days[0];
      for (const d of s.layout.days) { if (bt + s.layout.dayTop[d] <= probe) want = d; else break; }
    }
    if (want !== current) {
      current = want;
      const need = [want.slice(0, 4), want.slice(0, 7)];
      for (const k of Object.keys(auto)) if (!need.includes(k)) { setOpen(k, false, false); delete auto[k]; }
      for (const k of need) if (!isOpen[k]) { setOpen(k, true, false); auto[k] = true; }
      if (here) here.classList.remove("here");
      here = rowOf[want] || rowOf[want.slice(0, 7)];
      if (here) {
        here.classList.add("here");
        const r = here.getBoundingClientRect(), b = tl.getBoundingClientRect();
        if (r.top < b.top + 8) tl.scrollTop -= b.top + 8 - r.top; else if (r.bottom > b.bottom - 8) tl.scrollTop += r.bottom - b.bottom + 8;
      }
    }
    prefetch(s);
  }

  // The months either side of the one being looked at are fetched ahead, and the first few of their
  // thumbnails with them, so scrolling on or back is already there.
  function prefetch(s) {
    const i = order.indexOf(s.bucket);
    for (const j of [i - 1, i + 1]) {
      const n = secs[order[j]]; if (!n) continue;
      ensure(n, true).then(() => {
        if (!n.items || n.warmed) return;
        n.warmed = true;
        for (const it of n.items.slice(0, WARM)) new Image().src = thumbUrl(it.id);
      });
    }
  }

  // ---- the viewer ----
  const ZSTEP = 0.25, ZMAX = 4;       // zoom is relative to the fitted photo: 1 = whole photo in the window
  let pv, stage, side, vtitle, zoom = 1, infoOn = false, cur = null;      // cur = { s, i }
  function buildViewer() {
    pv = document.createElement("div"); pv.id = "pv"; pv.hidden = true;
    pv.innerHTML =
      '<div class="pv-bar"><button class="btn sec sm" id="pv-back" aria-label="Back to the timeline">&larr; Back</button>' +
      '<span id="pv-title" class="pv-title"></span><span class="sp"></span>' +
      '<button class="btn sec sm pv-ic" id="pv-fit" aria-label="Fit to window" title="Back to fit" hidden><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 9V4h5M15 4h5v5M20 15v5h-5M9 20H4v-5"/></svg></button>' +
      '<button class="btn sec sm pv-ic" id="pv-out" aria-label="Zoom out" title="Zoom out" hidden><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="10" cy="10" r="6"/><path d="M21 21l-6-6M7 10h6"/></svg></button>' +
      '<span id="pv-pct" class="pv-pct" hidden></span>' +
      '<button class="btn sec sm pv-ic" id="pv-in" aria-label="Zoom in" title="Zoom in"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="10" cy="10" r="6"/><path d="M21 21l-6-6M7 10h6M10 7v6"/></svg></button>' +
      '<button class="pv-i" id="pv-info" aria-label="Details" title="Details" aria-expanded="false">i</button></div>' +
      '<div class="pv-body"><div class="pv-stage" id="pv-stage"></div><aside class="pv-side" id="pv-side" hidden></aside></div>' +
      '<button class="pv-nav prev" id="pv-prev" aria-label="Previous photo">&lsaquo;</button><button class="pv-nav next" id="pv-next" aria-label="Next photo">&rsaquo;</button>';
    document.body.appendChild(pv);
    stage = $("pv-stage"); side = $("pv-side"); vtitle = $("pv-title");
    $("pv-back").onclick = closeViewer;
    $("pv-prev").onclick = () => step(-1);
    $("pv-next").onclick = () => step(1);
    $("pv-fit").onclick = () => setZoom(1);
    $("pv-out").onclick = () => setZoom(zoom - ZSTEP);
    $("pv-in").onclick = () => setZoom(zoom + ZSTEP);
    $("pv-info").onclick = () => { infoOn = !infoOn; side.hidden = !infoOn; $("pv-info").setAttribute("aria-expanded", String(infoOn)); if (infoOn) loadInfo(item()); applyZoom(true); };
    stage.addEventListener("wheel", e => { if (e.ctrlKey && !item().video) { e.preventDefault(); setZoom(zoom + (e.deltaY < 0 ? ZSTEP : -ZSTEP)); } }, { passive: false });
    let drag = null;       // drag to pan once zoomed in
    stage.addEventListener("pointerdown", e => { if (zoom <= 1) return; drag = { x: e.clientX, y: e.clientY, l: stage.scrollLeft, t: stage.scrollTop }; stage.setPointerCapture(e.pointerId); stage.classList.add("panning"); });
    stage.addEventListener("pointermove", e => { if (drag) { stage.scrollLeft = drag.l - (e.clientX - drag.x); stage.scrollTop = drag.t - (e.clientY - drag.y); } });
    const endDrag = () => { drag = null; stage.classList.remove("panning"); };
    stage.addEventListener("pointerup", endDrag); stage.addEventListener("pointercancel", endDrag);
    window.addEventListener("resize", () => { if (!pv.hidden) applyZoom(false); });
    document.addEventListener("keydown", e => {
      if (pv.hidden) return;
      if (e.key === "Escape") closeViewer(); else if (e.key === "ArrowLeft") step(-1); else if (e.key === "ArrowRight") step(1);
    });
  }
  const item = () => cur && cur.s.items[cur.i];
  function openViewer(s, i) { if (!pv) buildViewer(); if (i < 0) return; cur = { s, i }; pv.hidden = false; show(); }
  function closeViewer() { if (!pv || pv.hidden) return; pv.hidden = true; stage.innerHTML = ""; cur = null; }
  // Zoom works by sizing the picture to a multiple of its fitted width; the stage scrolls (and drags) to pan.
  function setZoom(z) { zoom = Math.min(ZMAX, Math.max(1, Math.round(z * 100) / 100)); applyZoom(true); }
  function applyZoom(keepCentre) {
    const img = stage.querySelector("img"), it = item();
    const vid = !it || it.video;
    $("pv-in").hidden = vid; $("pv-out").hidden = $("pv-fit").hidden = $("pv-pct").hidden = vid || zoom <= 1;
    $("pv-in").disabled = zoom >= ZMAX;
    $("pv-pct").textContent = Math.round(zoom * 100) + "%";
    stage.classList.toggle("can-pan", zoom > 1);
    if (!img) return;
    const fx = (stage.scrollLeft + stage.clientWidth / 2) / (stage.scrollWidth || 1), fy = (stage.scrollTop + stage.clientHeight / 2) / (stage.scrollHeight || 1);
    if (zoom <= 1) { img.style.width = ""; img.classList.remove("zoomed"); stage.scrollLeft = stage.scrollTop = 0; return; }
    const ratio = it.ratio || (img.naturalWidth && img.naturalHeight ? img.naturalWidth / img.naturalHeight : 1.5);
    img.classList.add("zoomed");
    img.style.width = Math.round(Math.min(stage.clientWidth, stage.clientHeight * ratio) * zoom) + "px";
    if (keepCentre) { stage.scrollLeft = fx * stage.scrollWidth - stage.clientWidth / 2; stage.scrollTop = fy * stage.scrollHeight - stage.clientHeight / 2; }
  }
  function show() {
    const it = item(); zoom = 1;
    vtitle.textContent = dayLabel(it.day) + (it.time ? " · " + it.time : "") + (it.owner ? " · " + it.owner : "");
    stage.className = "pv-stage";
    stage.innerHTML = it.video
      ? '<video controls autoplay playsinline src="/api/household/' + it.id + '/video"></video>'
      : '<img alt="" src="/api/household/' + it.id + '/preview">';
    applyZoom(false);
    edges();
    if (infoOn) loadInfo(it);
  }
  // Whether there is a photo before or after, looking across month boundaries.
  function edges() {
    const k = order.indexOf(cur.s.bucket);
    $("pv-prev").hidden = cur.i <= 0 && k <= 0;
    $("pv-next").hidden = cur.i >= cur.s.items.length - 1 && k >= order.length - 1;
  }
  // The next photo along the timeline, fetching the next month's list if it is not there yet.
  async function step(d) {
    if (!cur) return;
    let k = order.indexOf(cur.s.bucket), i = cur.i + d, s = cur.s;
    while (i < 0 || i >= s.items.length) {
      k += d; if (k < 0 || k >= order.length) return;
      s = secs[order[k]];
      await ensure(s, true);
      if (!s.items) return;
      i = d > 0 ? 0 : s.items.length - 1;           // an empty month falls through to the next one
    }
    if (!cur) return;
    cur = { s, i }; show();
  }
  async function loadInfo(it) {
    side.innerHTML = '<p class="muted">Loading&hellip;</p>';
    try {
      const r = await api("/api/household/" + it.id), m = r.meta || {}, rows = [];
      const add = (k, v) => { if (v) rows.push("<dt>" + k + "</dt><dd>" + esc(String(v)) + "</dd>"); };
      const size = [m.width && m.height ? m.width + " × " + m.height : "", m.size_bytes ? niceBytes(m.size_bytes) : ""].filter(Boolean).join(" · ");
      const exposure = [m.exposure_time ? m.exposure_time + " s" : "", m.f_number ? "f/" + m.f_number : "", m.iso ? "ISO " + m.iso : ""].filter(Boolean).join(" · ");
      add("File", r.file_name); add("Owner", m.owner_name); add("Taken", m.taken); add("Place", m.place);
      add("Camera", m.camera); add("Exposure", exposure); add("Size", size);
      if (item() === it) side.innerHTML = "<h4>Details</h4><dl>" + (rows.join("") || "<dd>No details.</dd>") + "</dl>";
    } catch (e) { side.innerHTML = '<p class="muted">Couldn\'t load details.</p>'; }
  }

  // ---- coming and going ----
  function show_() {
    if (!built) build();
    if (!started) start(); else { sc.scrollTop = savedTop; schedule(); follow(); }
  }
  function hide() {
    if (built && !pane.hidden) savedTop = sc.scrollTop;
    closeViewer();
  }
  return { show: show_, hide };
})();
