// The Photos view of the main page (#photos): a scrolling timeline of everyone's photos (those who
// partnered the pipeline account in Immich), with search, selection, a full-window viewer, and what
// can be done with the photos: send them to an entry queue, download them, move them to their
// owner's Immich trash. Pictures come through the pipeline's own proxy routes, never straight from
// Immich. Photos already in the pipeline are left out unless the search filters ask for them.
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
  let built = false, started = false, sc, tl, host, note, head, selbar, panel, resBox, resHead, rtl;
  let secs = {};            // bucket -> { bucket, month, count, el, head, body, items, layout, rendered, ... }
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

  // What the person has asked to see. Photos already in the pipeline are hidden unless showSent.
  const Q = { text: "", mode: "context", from: "", to: "", country: "", state: "", city: "", type: "", owner: "", make: "", model: "", lens: "",
    people: new Set(), favorites: false, archive: false, notInAlbum: false };
  let collage = false;       // the Collage Maker place is showing (it reuses the search-results machinery)
  let bestFirst = false;      // a search with text to match: results stay in Immich's order, best match first, with no months
  let showSent = false, searching = false, monthsStale = false, allResults = [], main = null, nextPage = null, loadingMore = false, searchToken = 0;
  const sel = new Set();     // selected photo ids
  const byId = new Map();    // photo id -> { it, s } for every photo loaded (its section, for the day and removals)
  const MODES = { context: "Context", file_name: "File name", description: "Description", path: "Full path or folder", ocr: "OCR" };
  const DESTS = [
    { key: "wallpaper", label: "Wallpaper Maker", sub: "create a matted wallpaper" },
    { key: "collage", label: "Collage Maker", sub: "create a collage from 2 or more photos" },
    { key: "import", label: "Import Only", sub: "import as-is, no recipe" }];
  const reg = (s, items) => { for (const it of items) byId.set(it.id, { it, s }); };
  const plural = (n, one, many) => n + " " + (n === 1 ? one : many || one + "s");

  function showNote(html) { note.innerHTML = html; note.hidden = !html; }

  // ---- building the view ----
  const PANEL = '<div class="ph-psec"><h5>Filter by</h5><div class="ph-chips" id="ph-fchips">' +
    '<button class="ph-ch on" data-sec="s-type" aria-pressed="true">Search type</button><button class="ph-ch" data-sec="s-people" aria-pressed="false">People</button>' +
    '<button class="ph-ch" data-sec="s-date" aria-pressed="false">Date</button><button class="ph-ch" data-sec="s-places" aria-pressed="false">Places</button>' +
    '<button class="ph-ch" data-sec="s-media" aria-pressed="false">Media</button></div>' +
    '<div class="ph-sec" id="s-type"><p>How your words are read: by what the photo looks like, its file name, its description, its folder, or text in the picture.</p><div class="ph-chips" id="ph-modes">' +
    Object.entries(MODES).map(([k, v]) => '<button class="ph-ch' + (k === "context" ? " on" : "") + '" data-mode="' + k + '">' + v + '</button>').join("") + '</div></div>' +
    '<div class="ph-sec" id="s-people" hidden><p>Show photos with these people, as Immich has named them.</p><div class="ph-chips" id="ph-people"><span class="muted">Loading&hellip;</span></div></div>' +
    '<div class="ph-sec" id="s-date" hidden><p>Photos taken in this range.</p><div class="ph-grid"><label>From<input type="date" id="f-from"></label><label>To<input type="date" id="f-to"></label></div></div>' +
    '<div class="ph-sec" id="s-places" hidden><p>Where the photo was taken.</p><div class="ph-grid"><label>Country<input id="f-country" list="dl-country" autocomplete="off"></label>' +
    '<label>State<input id="f-state" autocomplete="off"></label><label>City<input id="f-city" autocomplete="off"></label></div><datalist id="dl-country"></datalist></div>' +
    '<div class="ph-sec" id="s-media" hidden><p>Kinds of items, and whose they are.</p><div class="ph-grid"><label>Type<select id="f-type"><option value="">Photos and videos</option><option value="photo">Photos</option><option value="video">Videos</option></select></label>' +
    '<label>Whose photos<select id="f-owner"><option value="">Everyone</option></select></label></div></div></div>' +
    '<div class="ph-psec" id="ph-adv"><h5>Camera</h5><div class="ph-grid"><label>Make<input id="f-make" list="dl-make" autocomplete="off"></label><label>Model<input id="f-model" list="dl-model" autocomplete="off"></label>' +
    '<label>Lens model<input id="f-lens" list="dl-lens" autocomplete="off"></label></div><datalist id="dl-make"></datalist><datalist id="dl-model"></datalist><datalist id="dl-lens"></datalist>' +
    '<h5 style="margin-top:1rem">Library</h5><div class="ph-chips" id="ph-lib"><button class="ph-ch" data-flag="favorites" aria-pressed="false">Favorites</button>' +
    '<button class="ph-ch" data-flag="archive" aria-pressed="false">Archive</button><button class="ph-ch" data-flag="notInAlbum" aria-pressed="false">Not in album</button>' +
    '<button class="ph-ch" id="f-sent" aria-pressed="false" title="Photos already sent to the pipeline are hidden unless this is on">Show photos already in the pipeline</button></div></div>' +
    '<div class="ph-pfoot"><button class="ph-ch" id="ph-advt" aria-expanded="true">Advanced filters &#710;</button><span class="sp"></span>' +
    '<button class="btn sec sm" id="ph-reset">Clear all</button><button class="btn sm" id="ph-go">Search</button></div>';

  function build() {
    built = true;
    pane.innerHTML =
      '<div class="ph-top"><div class="card head" id="ph-head"><h2 id="ph-title">Photos</h2><span id="ph-count" class="chip"></span>' +
      '<div class="ph-search" id="ph-searchbox"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/></svg>' +
      '<input id="ph-q" placeholder="Search the household\'s photos" aria-label="Search" autocomplete="off">' +
      '<button id="ph-clear" class="ph-x" aria-label="Clear search" hidden>&times;</button>' +
      '<button id="ph-tune" class="ph-x" aria-label="Search filters" aria-expanded="false" aria-controls="ph-panel"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h10M18 7h2M4 17h2M10 17h10"/><circle cx="16" cy="7" r="2"/><circle cx="8" cy="17" r="2"/></svg></button></div></div>' +
      '<div class="card ph-selbar" id="ph-selbar" hidden><button id="ph-selclear" class="ph-x" aria-label="Clear selection">&times;</button><strong id="ph-selcount"></strong>' +
      '<button class="btn sec sm" id="ph-selday">Select day</button><span class="sp"></span><span id="ph-selwarn" class="ph-warn"></span>' +
      '<button class="btn sec sm" id="ph-seldl">Download</button><button class="btn warn sm" id="ph-seltrash">Trash</button><button class="btn sm" id="ph-selsend" aria-haspopup="menu">Send to &#9662;</button>' +
      '<button class="btn sec sm" id="ph-selremove" hidden>Remove from Collage Maker</button><button class="btn sm" id="ph-selmake" hidden>Make collage (select 2 or more)</button></div>' +
      '<div class="ph-panel" id="ph-panel" role="dialog" aria-label="Search filters" hidden>' + PANEL + '</div></div>' +
      '<div class="ph-wrap"><div class="ph-scroll" id="ph-scroll"><div id="ph-note" class="ph-note" hidden></div>' +
      '<div id="ph-res" class="ph-resline" hidden><span id="ph-restext"></span><button class="btn sec sm" id="ph-resclear">Clear search</button></div>' +
      '<div id="ph-results" hidden></div><div id="ph-months"></div></div>' +
      '<div class="ph-tl" id="ph-tl" role="tree" aria-label="Timeline"></div><div class="ph-tl" id="ph-rtl" role="tree" aria-label="Timeline of the results" hidden></div></div>';
    sc = $("ph-scroll"); tl = $("ph-tl"); rtl = $("ph-rtl"); host = $("ph-months"); note = $("ph-note");
    head = $("ph-head"); selbar = $("ph-selbar"); panel = $("ph-panel"); resBox = $("ph-results"); resHead = $("ph-res");
    sc.addEventListener("scroll", () => { schedule(); clearTimeout(followTimer); followTimer = setTimeout(follow, FOLLOW_MS); }, { passive: true });
    let rz = null;
    window.addEventListener("resize", () => { if (pane.hidden) return; clearTimeout(rz); rz = setTimeout(relayoutAll, 150); });
    wireSearch(); wireSelection();
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
    s.promise = api("/api/household/month?bucket=" + encodeURIComponent(s.bucket) + (showSent ? "&sent=1" : ""))
      .then(d => { s.items = d.items; reg(s, s.items); s.state = "ready"; layoutSection(s); return s; })
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
    if (s.flat) days.push({ day: "", items: s.items });          // best match first: one run of tiles, no day headers
    else for (const it of s.items) { const d = days[days.length - 1]; if (d && d.day === it.day) d.items.push(it); else days.push({ day: it.day, items: [it] }); }
    for (const d of days) {
      dayTop[d.day] = y;
      if (!s.flat) { rows.push({ kind: "day", top: y, h: DAYH, label: dayLabel(d.day), ids: d.items.map(i => i.id) }); y += DAYH; }
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
    if (!built || pane.hidden) return;
    const box = sc.getBoundingClientRect();
    if (!order.length) return;
    for (const b of order) {
      const s = secs[b], r = s.el.getBoundingClientRect();
      if (r.bottom > box.top - DATA_NEAR && r.top < box.bottom + DATA_NEAR && !s.items) ensure(s);
      const near = r.bottom > box.top - TILE_NEAR && r.top < box.bottom + TILE_NEAR;
      if (near && s.items) drawRows(s, box); else if (s.rendered.size) clearRows(s);
    }
    if (searching && nextPage && !loadingMore) {            // the end of the results is close: fetch the next page
      const last = secs[order[order.length - 1]];
      if (last && last.el.getBoundingClientRect().bottom < box.bottom + DATA_NEAR) loadMore();
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
    if (row.kind === "day") {
      el.className = "ph-day";
      const dp = document.createElement("span");
      dp.className = "ph-dpick" + (row.ids.every(id => sel.has(id)) ? " on" : ""); dp._ids = row.ids;
      dp.setAttribute("role", "checkbox"); dp.setAttribute("aria-label", "Select all photos of " + row.label);
      dp.onclick = () => { const all = row.ids.every(id => sel.has(id)); for (const id of row.ids) all ? sel.delete(id) : sel.add(id); paintSel(); };
      el.appendChild(dp); el.appendChild(document.createTextNode(row.label));
      return el;
    }
    el.className = "ph-row"; el.style.height = row.h + "px";
    for (const t of row.tiles) el.appendChild(tile(s, t.it, t.w, row.h));
    return el;
  }

  function tile(s, it, w, h) {
    const a = document.createElement("button");
    a.className = "ph-tile" + (sel.has(it.id) ? " sel" : ""); a.style.width = w + "px"; a.style.height = h + "px"; a.dataset.id = it.id;
    a.setAttribute("aria-label", (it.video ? "Video" : "Photo") + (it.owner ? " from " + it.owner : "") + ", " + it.day);
    const img = document.createElement("img"); img.alt = ""; img.decoding = "async";
    img.onload = () => img.classList.add("in");
    img.src = thumbUrl(it.id);
    if (img.complete && img.naturalWidth) img.classList.add("in");
    a.appendChild(img);
    const pk = document.createElement("span"); pk.className = "ph-pick"; pk.setAttribute("role", "checkbox"); pk.setAttribute("aria-label", "Select"); pk.textContent = sel.has(it.id) ? "✓" : "";
    pk.onclick = e => { e.stopPropagation(); toggleSel(it.id); };
    a.appendChild(pk);
    if (it.pipeline) { const g = document.createElement("span"); g.className = "ph-tag" + (it.pipeline === "trash" ? " trash" : ""); g.textContent = it.pipeline === "trash" ? "In trash" : "In pipeline"; a.appendChild(g); }
    if (it.owner) { const o = document.createElement("span"); o.className = "ph-owner"; o.textContent = it.owner; a.appendChild(o); }
    if (it.video) { const v = document.createElement("span"); v.className = "ph-dur"; v.textContent = "▶ " + dur(it.seconds || 0); a.appendChild(v); }
    a.onclick = () => openViewer(s, s.items.indexOf(it));
    a.onkeydown = e => { if (e.key === " ") { e.preventDefault(); toggleSel(it.id); } };      // Space selects, Enter opens
    a.onkeyup = e => { if (e.key === " ") e.preventDefault(); };
    return a;
  }

  // ---- the timeline: year > month > day ----
  let rowOf = {}, kidsOf = {}, isOpen = {}, auto = {};
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
    if (!s || s.flat) return;
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
      '<button class="btn sm" id="pv-send" aria-haspopup="menu">Send to &#9662;</button>' +
      '<button class="btn sec sm" id="pv-pick" hidden>Select for collage</button><button class="btn sec sm" id="pv-remove" hidden>Remove from Collage Maker</button>' +
      '<button class="btn sec sm pv-ic" id="pv-fit" aria-label="Fit to window" title="Back to fit" hidden><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 9V4h5M15 4h5v5M20 15v5h-5M9 20H4v-5"/></svg></button>' +
      '<button class="btn sec sm pv-ic" id="pv-out" aria-label="Zoom out" title="Zoom out" hidden><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="10" cy="10" r="6"/><path d="M21 21l-6-6M7 10h6"/></svg></button>' +
      '<span id="pv-pct" class="pv-pct" hidden></span>' +
      '<button class="btn sec sm pv-ic" id="pv-in" aria-label="Zoom in" title="Zoom in"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="10" cy="10" r="6"/><path d="M21 21l-6-6M7 10h6M10 7v6"/></svg></button>' +
      '<button class="btn sec sm pv-ic" id="pv-dl" aria-label="Download" title="Download"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v11m-5-4 5 5 5-5M5 20h14"/></svg></button>' +
      '<button class="pv-i" id="pv-info" aria-label="Details" title="Details" aria-expanded="false">i</button>' +
      '<button class="btn sec sm pv-ic" id="pv-trash" aria-label="Move to trash" title="Move to trash"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/></svg></button></div>' +
      '<div class="pv-body"><div class="pv-stage" id="pv-stage"></div><aside class="pv-side" id="pv-side" hidden></aside></div>' +
      '<button class="pv-nav prev" id="pv-prev" aria-label="Previous photo">&lsaquo;</button><button class="pv-nav next" id="pv-next" aria-label="Next photo">&rsaquo;</button>';
    document.body.appendChild(pv);
    stage = $("pv-stage"); side = $("pv-side"); vtitle = $("pv-title");
    $("pv-back").onclick = backFromViewer;
    $("pv-prev").onclick = () => step(-1);
    $("pv-next").onclick = () => step(1);
    $("pv-send").onclick = e => sendMenu(e.currentTarget, [item().id]);
    $("pv-pick").onclick = () => { toggleSel(item().id); paintPick(); };
    $("pv-remove").onclick = () => removeFromCollage([item().id]);
    $("pv-dl").onclick = () => download([item().id]);
    $("pv-trash").onclick = () => trashFlow([item().id]);
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
      if (document.querySelector(".ph-modalback") || sendEl) return;                  // a dialog or menu has the keys first
      if (e.key === "Escape") backFromViewer(); else if (e.key === "ArrowLeft") step(-1); else if (e.key === "ArrowRight") step(1);
    });
  }
  const item = () => cur && cur.s.items[cur.i];
  const flatItems = () => order.flatMap(b => secs[b].items || []);
  // The viewer takes one step in the browser's history, so the browser's Back button closes it and leaves the
  // timeline where it was, the same as the Back button on the viewer. Stepping between photos adds nothing.
  let pushed = false;
  function openViewer(s, i) {
    if (!pv) buildViewer(); if (i < 0) return;
    if (pv.hidden) { try { history.pushState({ phViewer: 1 }, ""); pushed = true; } catch (e) { /* no history: Back just won't be caught */ } }
    cur = { s, i }; pv.hidden = false; show();
  }
  function closeViewer() { if (!pv || pv.hidden) return; pv.hidden = true; stage.innerHTML = ""; cur = null; }
  // Back button, Escape: also gives back the history step the viewer took.
  function backFromViewer() { if (pushed) { pushed = false; closeViewer(); history.back(); } else closeViewer(); }
  window.addEventListener("popstate", () => { if (pv && !pv.hidden) { pushed = false; closeViewer(); } else pushed = false; });
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
    vtitle.textContent = dayLabel(it.day) + (it.time ? " · " + it.time : "") + (it.owner ? " · " + it.owner : "") +
      (it.pipeline ? " · " + (it.pipeline === "trash" ? "in the pipeline's trash" : "in the pipeline") : "");
    stage.className = "pv-stage";
    stage.innerHTML = it.video
      ? '<video controls autoplay playsinline src="/api/household/' + it.id + '/video"></video>'
      : '<img alt="" src="/api/household/' + it.id + '/preview">';
    applyZoom(false);
    edges();
    $("pv-send").hidden = $("pv-dl").hidden = $("pv-trash").hidden = collage;       // Collage Maker: select, remove, zoom and details only
    $("pv-pick").hidden = $("pv-remove").hidden = !collage;
    if (collage) paintPick();
    if (infoOn) loadInfo(it);
  }
  function paintPick() {
    const on = sel.has(item().id), b = $("pv-pick");
    b.textContent = on ? "Selected ✓" : "Select for collage"; b.classList.toggle("sec", !on); b.setAttribute("aria-pressed", String(on));
  }
  // Whether there is a photo before or after, looking across month boundaries.
  function edges() {
    if (cur.s.results) { const f = flatItems(), j = f.indexOf(item()); $("pv-prev").hidden = j <= 0; $("pv-next").hidden = j >= f.length - 1 && !nextPage; return; }
    const k = order.indexOf(cur.s.bucket);
    $("pv-prev").hidden = cur.i <= 0 && k <= 0;
    $("pv-next").hidden = cur.i >= cur.s.items.length - 1 && k >= order.length - 1;
  }
  // The next photo along the timeline, fetching the next month's list if it is not there yet.
  async function step(d) {
    if (!cur) return;
    if (cur.s.results) {                              // search results: along the list, fetching the next page at its end
      const here_ = item();
      let f = flatItems(), j = f.indexOf(here_) + d;
      if (j >= f.length && nextPage && !loadingMore) { await loadMore(); f = flatItems(); j = f.indexOf(here_) + d; }
      if (!cur || j < 0 || j >= f.length) return;
      const e = byId.get(f[j].id);
      cur = { s: e.s, i: e.s.items.indexOf(f[j]) }; show(); return;
    }
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

  // ---- search ----
  // The words go in the box at the top; the button beside it opens the filters. Results replace the
  // months (and the timeline) until the search is cleared. Photos already in the pipeline stay out of
  // both unless "Show photos already in the pipeline" is on.
  let facetsLoaded = false;
  const field = id => $(id);
  function readForm() {
    Q.text = $("ph-q").value.trim();
    for (const [k, id] of [["from", "f-from"], ["to", "f-to"], ["country", "f-country"], ["state", "f-state"], ["city", "f-city"],
      ["type", "f-type"], ["owner", "f-owner"], ["make", "f-make"], ["model", "f-model"], ["lens", "f-lens"]]) Q[k] = field(id).value.trim();
  }
  function hasSearch() {
    return !!(Q.text || Q.from || Q.to || Q.country || Q.state || Q.city || Q.type || Q.owner || Q.make || Q.model || Q.lens ||
      Q.people.size || Q.favorites || Q.archive || Q.notInAlbum);
  }
  function searchUrl(page) {
    const p = new URLSearchParams({ q: Q.text, mode: Q.mode, page: String(page) });
    for (const [k, v] of [["from", Q.from], ["to", Q.to], ["country", Q.country], ["state", Q.state], ["city", Q.city], ["type", Q.type],
      ["owner", Q.owner], ["make", Q.make], ["model", Q.model], ["lens", Q.lens]]) if (v) p.set(k, v);
    if (Q.people.size) p.set("people", [...Q.people].join(","));
    if (Q.favorites) p.set("favorites", "1");
    if (Q.archive) p.set("archive", "1");
    if (Q.notInAlbum) p.set("not_in_album", "1");
    if (showSent) p.set("sent", "1");
    return "/api/household/search?" + p;
  }
  function summary() {
    const bits = [];
    if (Q.text) bits.push("“" + Q.text + "” (" + MODES[Q.mode].toLowerCase() + ")");
    if (Q.people.size) bits.push(plural(Q.people.size, "person", "people"));
    if (Q.from || Q.to) bits.push((Q.from || "…") + " to " + (Q.to || "now"));
    const where = [Q.city, Q.state, Q.country].filter(Boolean).join(", ");
    if (where) bits.push(where);
    if (Q.type) bits.push(Q.type === "video" ? "videos" : "photos");
    if (Q.owner) { const o = $("f-owner"); bits.push(o.selectedOptions[0] ? o.selectedOptions[0].textContent : "one person"); }
    for (const v of [Q.make, Q.model, Q.lens]) if (v) bits.push(v);
    if (Q.favorites) bits.push("favorites");
    if (Q.archive) bits.push("archive");
    if (Q.notInAlbum) bits.push("not in an album");
    return bits.join(" · ");
  }
  function resultText() {
    const n = allResults.length;
    return (n ? plural(n, "result") + (nextPage ? " so far" : "") : "No results") + (summary() ? " for " + summary() : "") + (bestFirst ? ". Best match first." : ". Newest first on each page.");
  }

  // A search shows its results the way the main page shows the timeline: by month and day, with a timeline of
  // its own. The main page's state is put aside while searching and put back when the search is cleared.
  function enterSearch() {
    if (!main) main = { secs, order, rowOf, kidsOf, isOpen, auto, current, here, host, tl };
    secs = {}; order = []; rowOf = {}; kidsOf = {}; isOpen = {}; auto = {}; current = null; here = null;
    host = resBox; tl = rtl; allResults = []; bestFirst = false;
    host.innerHTML = ""; tl.innerHTML = "";
    $("ph-months").hidden = true; $("ph-tl").hidden = true; resBox.hidden = false; rtl.hidden = false;
  }
  function leaveSearch() {
    if (!main) return;
    ({ secs, order, rowOf, kidsOf, isOpen, auto, current, here, host, tl } = main); main = null;
    resBox.hidden = true; rtl.hidden = true; resBox.innerHTML = ""; rtl.innerHTML = "";
    host.hidden = false; tl.hidden = false;
    for (const k of Object.keys(secs)) if (secs[k].items) reg(secs[k], secs[k].items);     // a result that was also on the timeline had taken its entry
  }
  async function runSearch() {
    readForm(); closePanel();
    if (!hasSearch()) { await clearSearch(false); return; }
    const token = ++searchToken;
    searching = true; sel.clear();
    for (const [id, e] of byId) if (e.s.results) byId.delete(id);
    enterSearch(); resHead.hidden = false;
    bestFirst = Q.mode === "context" && !!Q.text;
    $("ph-clear").hidden = false;
    nextPage = null;
    $("ph-restext").textContent = "Searching…";
    sc.scrollTop = 0; closeViewer(); paintSel();
    try {
      const d = await api(searchUrl(1));
      if (token !== searchToken) return;
      addResults(d);
    } catch (e) {
      if (token !== searchToken) return;
      $("ph-restext").textContent = "Couldn't search Immich. " + (e.message || "");
    }
  }
  function addResults(d) {
    nextPage = d.next || null;
    allResults = allResults.concat(d.items);
    if (!bestFirst) allResults.sort((a, b) => (b.day + b.time).localeCompare(a.day + a.time));
    rebuildResults();
    $("ph-restext").textContent = resultText();
    schedule();
  }
  // Groups everything found so far into months (newest first) and lays them out like the main page.
  function rebuildResults() {
    const top = sc.scrollTop;
    for (const [id, e] of byId) if (e.s.results) byId.delete(id);
    for (const k of Object.keys(secs)) delete secs[k];
    for (const o of [rowOf, kidsOf, isOpen, auto]) for (const k of Object.keys(o)) delete o[k];
    host.innerHTML = ""; tl.innerHTML = ""; order = []; current = null; here = null;
    if (bestFirst) {                      // one section, in the order Immich gave, with no months and no timeline
      const el = document.createElement("section"); el.className = "ph-month"; el.innerHTML = '<div class="ph-body"></div>';
      secs.rbest = { bucket: "rbest", month: "", count: allResults.length, el, head: null, body: el.firstChild, items: allResults, flat: true,
        layout: null, rendered: new Map(), state: "ready", promise: null, failed: false, warmed: true, results: true };
      order = ["rbest"]; host.appendChild(el); tl.hidden = true;
      reg(secs.rbest, allResults); layoutSection(secs.rbest);
      sc.scrollTop = top; schedule();
      return;
    }
    tl.hidden = false;
    const groups = new Map();
    for (const it of allResults) {
      if (!it.day) continue;
      const m = it.day.slice(0, 7);
      if (!groups.has(m)) groups.set(m, []);
      groups.get(m).push(it);
    }
    const months = [...groups.keys()].sort().reverse().map(m => ({ bucket: "r" + m, month: m, count: groups.get(m).length }));
    order = months.map(m => m.bucket);
    for (const m of months) {
      const el = document.createElement("section"); el.className = "ph-month";
      el.innerHTML = '<h3 class="ph-mh">' + esc(monthLabel(m.month)) + ' <span class="muted">' + m.count.toLocaleString() + '</span></h3><div class="ph-body"></div>';
      secs[m.bucket] = { bucket: m.bucket, month: m.month, count: m.count, el, head: el.firstChild, body: el.lastChild, items: groups.get(m.month),
        layout: null, rendered: new Map(), state: "ready", promise: null, failed: false, warmed: true, results: true };
      host.appendChild(el);
    }
    buildTimeline(months);
    for (const m of months) { const s = secs[m.bucket]; reg(s, s.items); layoutSection(s); }
    sc.scrollTop = top;
    schedule(); follow();
  }
  async function loadMore() {
    if (!searching || !nextPage || loadingMore) return;
    loadingMore = true; const token = searchToken, page = nextPage;
    try { const d = await api(searchUrl(page)); if (token === searchToken) addResults(d); }
    catch (e) { if (token === searchToken) { nextPage = null; toast("Couldn't load more results."); } }
    finally { loadingMore = false; }
  }
  async function clearSearch(reset) {
    searchToken++;
    if (reset !== false) {
      $("ph-q").value = "";
      for (const id of ["f-from", "f-to", "f-country", "f-state", "f-city", "f-type", "f-owner", "f-make", "f-model", "f-lens"]) field(id).value = "";
      Q.people.clear(); Q.favorites = Q.archive = Q.notInAlbum = false; Q.mode = "context";
      for (const b of panel.querySelectorAll("[data-flag],[data-person]")) { b.classList.remove("on"); b.setAttribute("aria-pressed", "false"); }
      for (const b of $("ph-modes").children) b.classList.toggle("on", b.dataset.mode === "context");
    }
    for (const [id, e] of byId) if (e.s.results) byId.delete(id);
    sel.clear(); allResults = []; nextPage = null; searching = false;
    leaveSearch(); resHead.hidden = true; $("ph-clear").hidden = true; $("ph-months").hidden = false;
    closeViewer(); paintSel();
    if (monthsStale) { monthsStale = false; resetMonths(); } else { schedule(); follow(); }
  }
  // The months are fetched again, because what they list depends on "show photos already in the pipeline".
  function resetMonths() {
    for (const k of Object.keys(secs)) delete secs[k];
    for (const o of [rowOf, kidsOf, isOpen, auto]) for (const k of Object.keys(o)) delete o[k];
    for (const [id, e] of byId) if (!e.s.results) byId.delete(id);
    sel.clear(); order = []; current = null; here = null; started = false;
    host.innerHTML = ""; tl.innerHTML = ""; sc.scrollTop = 0; savedTop = 0;
    paintSel(); start();
  }
  function setShowSent(on) {
    showSent = on;
    const b = $("f-sent"); b.classList.toggle("on", on); b.setAttribute("aria-pressed", String(on));
    if (searching) { monthsStale = true; runSearch(); } else resetMonths();
  }

  function openPanel() { panel.hidden = false; $("ph-tune").setAttribute("aria-expanded", "true"); loadFacets(); }
  function closePanel() { if (!panel) return; panel.hidden = true; $("ph-tune").setAttribute("aria-expanded", "false"); }
  function loadFacets() {
    if (facetsLoaded) return;
    facetsLoaded = true;
    api("/api/household/filters").then(f => {
      $("f-owner").innerHTML = '<option value="">Everyone</option>' + f.owners.map(o => '<option value="' + esc(o.id) + '">' + esc(o.name) + "</option>").join("");
      $("ph-people").innerHTML = f.people.length
        ? f.people.map(p => '<button class="ph-ch" data-person="' + esc(p.id) + '" aria-pressed="false">' + esc(p.name) + "</button>").join("")
        : '<span class="muted">No named people yet. They appear here once Immich has named some.</span>';
    }).catch(() => { $("ph-people").innerHTML = '<span class="muted">Couldn\'t read the people from Immich.</span>'; });
    for (const [kind, list] of [["country", "dl-country"], ["camera-make", "dl-make"], ["camera-model", "dl-model"], ["camera-lens-model", "dl-lens"]]) {
      api("/api/household/suggest?type=" + kind).then(d => { $(list).innerHTML = d.values.map(v => '<option value="' + esc(v) + '">').join(""); }).catch(() => {});
    }
  }
  function wireSearch() {
    $("ph-q").onkeydown = e => { if (e.key === "Enter") runSearch(); else if (e.key === "Escape") closePanel(); };
    $("ph-tune").onclick = () => panel.hidden ? openPanel() : closePanel();
    $("ph-clear").onclick = () => clearSearch(true);
    $("ph-resclear").onclick = () => clearSearch(true);
    $("ph-go").onclick = runSearch;
    $("ph-reset").onclick = () => { const was = hasSearch(); clearSearch(true); if (showSent) setShowSent(false); closePanel(); return was; };
    $("ph-advt").onclick = () => { const on = $("ph-adv").hidden; $("ph-adv").hidden = !on; $("ph-advt").setAttribute("aria-expanded", String(on)); $("ph-advt").classList.toggle("on", on); };
    $("ph-fchips").onclick = e => {
      const b = e.target.closest("button"); if (!b) return;
      const on = b.classList.toggle("on"); b.setAttribute("aria-pressed", String(on)); $(b.dataset.sec).hidden = !on;
    };
    $("ph-modes").onclick = e => {
      const b = e.target.closest("button"); if (!b) return;
      Q.mode = b.dataset.mode; for (const x of $("ph-modes").children) x.classList.toggle("on", x === b);
    };
    panel.addEventListener("click", e => {
      const b = e.target.closest("button"); if (!b) return;
      if (b.dataset.flag) { const on = b.classList.toggle("on"); b.setAttribute("aria-pressed", String(on)); Q[b.dataset.flag] = on; }
      else if (b.dataset.person) {
        const on = b.classList.toggle("on"); b.setAttribute("aria-pressed", String(on));
        on ? Q.people.add(b.dataset.person) : Q.people.delete(b.dataset.person);
      } else if (b.id === "f-sent") setShowSent(!showSent);
    });
    panel.addEventListener("keydown", e => { if (e.key === "Enter" && e.target.tagName === "INPUT") runSearch(); else if (e.key === "Escape") closePanel(); });
    document.addEventListener("mousedown", e => {
      if (!panel.hidden && !panel.contains(e.target) && !head.contains(e.target)) closePanel();
    }, true);
  }

  // ---- selection ----
  // A circle on each photo (and beside each day) selects; a click on the photo itself opens the viewer.
  function toggleSel(id) { sel.has(id) ? sel.delete(id) : sel.add(id); paintSel(); }
  const selItems = () => [...sel].map(id => byId.get(id)).filter(Boolean).map(e => e.it);
  function paintSel() {
    if (!built) return;
    pane.classList.toggle("ph-selecting", sel.size > 0);
    for (const t of pane.querySelectorAll(".ph-tile")) {
      const on = sel.has(t.dataset.id); t.classList.toggle("sel", on);
      const pk = t.querySelector(".ph-pick"); if (pk) pk.textContent = on ? "✓" : "";
    }
    for (const d of pane.querySelectorAll(".ph-dpick")) d.classList.toggle("on", d._ids.every(id => sel.has(id)));
    const items = selItems(), n = items.length;
    selbar.hidden = n === 0; head.hidden = n > 0;
    if (n) closePanel();
    $("ph-selcount").textContent = n + " selected";
    const inTrash = items.filter(i => i.pipeline === "trash").length, inPipe = items.filter(i => i.pipeline === "in").length;
    $("ph-selwarn").textContent = [inPipe ? plural(inPipe, "photo") + " already in the pipeline will not be sent" : "",
      inTrash ? inTrash + " in the pipeline's trash: Send to will ask to purge " + (inTrash === 1 ? "it" : "them") + " first" : ""].filter(Boolean).join(" · ");
    if (collage) {
      const mk = $("ph-selmake");
      mk.disabled = n < COLLAGE_MIN || n > COLLAGE_MAX;
      mk.textContent = n < COLLAGE_MIN ? "Make collage (select " + COLLAGE_MIN + " or more)" : n > COLLAGE_MAX ? "Make collage (" + COLLAGE_MAX + " at most)" : "Make collage from " + n;
      $("ph-selwarn").textContent = n > COLLAGE_MAX ? "A collage takes " + COLLAGE_MAX + " photos at most. Deselect " + (n - COLLAGE_MAX) + "." : "";
      if (pv && !pv.hidden && cur) paintPick();
    }
  }
  function wireSelection() {
    $("ph-selclear").onclick = () => { sel.clear(); paintSel(); };
    $("ph-selday").onclick = () => {                      // every photo of the days that have a selected photo
      const days = new Set(selItems().map(i => i.day + "|" + byId.get(i.id).s.bucket));
      for (const [id, e] of byId) if (days.has(e.it.day + "|" + e.s.bucket)) sel.add(id);
      paintSel();
    };
    $("ph-selsend").onclick = e => sendMenu(e.currentTarget, [...sel]);
    $("ph-seldl").onclick = () => download([...sel]);
    $("ph-seltrash").onclick = () => trashFlow([...sel]);
    $("ph-selmake").onclick = makeCollage;
    $("ph-selremove").onclick = () => removeFromCollage([...sel]);
  }

  // ---- Collage Maker ----
  // The photos waiting in the Collage Maker album, shown like search results (by month and day, with a timeline).
  // Nothing is grouped for the person: they select 2 to 12 that belong together and press Make collage. The photos
  // are laid out in the order they were selected. Photos left unselected stay here.
  const COLLAGE_MIN = 2, COLLAGE_MAX = 12;
  const collageText = () => allResults.length
    ? plural(allResults.length, "photo") + " waiting in Collage Maker. Select the ones that belong together (" + COLLAGE_MIN + " to " + COLLAGE_MAX +
      "), then make a collage. Photos you leave unselected stay here."
    : "Nothing is waiting in Collage Maker. Send photos here with Send to in Photos.";
  function setCollageMode(on) {
    collage = on; pane.classList.toggle("ph-collage", on);
    $("ph-title").textContent = on ? "Collage Maker" : "Photos";
    $("ph-searchbox").hidden = on; $("ph-count").hidden = on; $("ph-resclear").hidden = on;
    for (const id of ["ph-selday", "ph-seldl", "ph-seltrash", "ph-selsend"]) $(id).hidden = on;
    $("ph-selmake").hidden = $("ph-selremove").hidden = !on;
  }
  async function enterCollage() {
    if (collage) return refreshCollage();
    if (searching) await clearSearch(true);
    savedTop = sc.scrollTop; sel.clear(); closeViewer(); closePanel();
    setCollageMode(true);
    searching = true; nextPage = null;
    enterSearch(); resHead.hidden = false; sc.scrollTop = 0; paintSel();
    await refreshCollage();
  }
  function leaveCollage() {
    if (!collage) return;
    setCollageMode(false);
    clearSearch(false);
  }
  async function refreshCollage() {
    const token = ++searchToken;
    $("ph-restext").textContent = "Looking in Collage Maker…";
    try {
      const d = await api("/api/collage/waiting");
      if (token !== searchToken || !collage) return;
      allResults = d.items; nextPage = null;
      const ids = new Set(allResults.map(i => i.id));
      for (const id of [...sel]) if (!ids.has(id)) sel.delete(id);
      rebuildResults(); paintSel();
      $("ph-restext").textContent = collageText();
    } catch (e) {
      if (token === searchToken) $("ph-restext").textContent = "Couldn't read Collage Maker from Immich. " + (e.message || "");
    }
  }
  // Photos that have left Collage Maker (made into a collage, or removed) go from the page; the sidebar count follows.
  function collageGone(ids) {
    const g = new Set(ids), here_ = cur && item() ? item().id : null;
    allResults = allResults.filter(i => !g.has(i.id));
    for (const id of ids) sel.delete(id);
    if (here_ && g.has(here_)) backFromViewer();
    rebuildResults(); paintSel();
    $("ph-restext").textContent = collageText();
    document.dispatchEvent(new Event("collage-changed"));
  }
  async function makeCollage() {
    const ids = [...sel];
    if (ids.length < COLLAGE_MIN || ids.length > COLLAGE_MAX) return;
    $("ph-selmake").disabled = true;
    const out = await post("/api/collage/make", { ids });
    if (!out) { await refreshCollage(); return; }
    toast("Sent to Claude. The collage will appear in Review when it is done.");
    collageGone(ids);
  }
  async function removeFromCollage(ids) {
    if (!ids.length) return;
    const out = await post("/api/collage/remove", { ids });
    if (!out) { await refreshCollage(); return; }
    const why = {};
    for (const s of out.skipped || []) why[s.reason] = (why[s.reason] || 0) + 1;
    const skipped = Object.entries(why).map(([r, n]) => n + " " + r).join(", ");
    toast((out.done.length ? "Removed from Collage Maker. The photos stay in the owner's Immich library." : "") +
      (skipped ? (out.done.length ? " " : "") + "Not removed: " + skipped + "." : ""), !out.done.length && !!skipped);
    if (out.done.length) collageGone(out.done);
    if (out.skipped && out.skipped.length) await refreshCollage();
  }

  // ---- what can be done with photos ----
  async function post(path, payload) {
    try {
      const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      const out = await r.json().catch(() => ({}));
      if (!r.ok) { toast(out.error || "That didn't work.", true); return null; }
      return out;
    } catch (e) { toast("That didn't work: " + (e.message || e), true); return null; }
  }
  function report(out, doneText) {
    const why = {};
    for (const s of out.skipped || []) why[s.reason] = (why[s.reason] || 0) + 1;
    const skipped = Object.entries(why).map(([r, n]) => n + " " + r).join(", ");
    toast((out.done.length ? doneText + (skipped ? "; " : ".") : "") + (skipped ? "not done: " + skipped + "." : ""), !out.done.length && !!skipped);
  }
  // Removes photos from the page (trashed, or sent and hidden), keeping the layout and the viewer in step.
  function dropItems(ids) {
    const curId = cur && item() ? item().id : null, touched = new Set();
    if (searching) { const gone = new Set(ids); allResults = allResults.filter(i => !gone.has(i.id)); }
    for (const id of ids) {
      const e = byId.get(id); if (!e) continue;
      e.s.items = e.s.items.filter(i => i.id !== id); byId.delete(id); sel.delete(id); touched.add(e.s);
    }
    for (const s of touched) layoutSection(s);
    if (curId && !byId.has(curId)) closeViewer();
    paintSel(); schedule();
  }
  function markSent(ids) {
    const touched = new Set();
    for (const id of ids) { const e = byId.get(id); if (e) { e.it.pipeline = "in"; touched.add(e.s); } }
    if (!showSent) { dropItems(ids); return; }
    for (const s of touched) layoutSection(s);
    for (const id of ids) sel.delete(id);
    paintSel(); schedule();
  }

  let sendEl = null;
  function closeSendMenu() { if (sendEl) { sendEl.remove(); sendEl = null; } }
  function sendMenu(anchor, ids) {
    closeSendMenu();
    if (!ids.length) return;
    const hasVideo = ids.some(id => byId.has(id) && byId.get(id).it.video);
    const m = document.createElement("div"); m.className = "ph-menu"; m.setAttribute("role", "menu"); sendEl = m;
    m.innerHTML = DESTS.map(d => {
      const off = d.key === "collage" && hasVideo;
      return '<button role="menuitem" data-dest="' + d.key + '"' + (off ? " disabled" : "") + "><span>" + d.label + "</span><small>" + d.sub + "</small>" +
        (off ? '<small class="why">a video can\'t go in a collage</small>' : "") + "</button>";
    }).join("");
    for (const b of m.querySelectorAll("button")) b.onclick = () => { const d = DESTS.find(x => x.key === b.dataset.dest); closeSendMenu(); doSend(ids, d); };
    document.body.appendChild(m);
    const r = anchor.getBoundingClientRect(), w = m.offsetWidth, h = m.offsetHeight;
    m.style.left = Math.max(8, Math.min(r.right - w, innerWidth - w - 8)) + "px";
    m.style.top = (r.bottom + 8 + h > innerHeight && r.top - 8 - h > 0 ? r.top - 8 - h : r.bottom + 8) + "px";
    m.querySelector("button:not(:disabled)").focus();
  }
  document.addEventListener("mousedown", e => { if (sendEl && !sendEl.contains(e.target)) closeSendMenu(); }, true);
  document.addEventListener("keydown", e => { if (e.key === "Escape" && sendEl) { e.stopPropagation(); closeSendMenu(); } }, true);

  async function doSend(ids, dest) {
    const trashed = ids.filter(id => byId.has(id) && byId.get(id).it.pipeline === "trash");
    if (trashed.length) {
      const one = trashed.length === 1;
      const ok = await ask({
        title: (one ? "This photo is" : trashed.length + " photos are") + " in the pipeline's trash",
        text: "A photo that is still in the pipeline can't be sent again. Send to " + dest.label + " and purge " + (one ? "its" : "their") +
          " processed photo from the trash so " + (one ? "it becomes" : "they become") + " eligible again? The processed photo made from " + (one ? "it" : "each") +
          " is deleted for good (for a collage, the whole collage), but only for a photo that was sent. " +
          "The original stays in its owner's Immich library.",
        yes: "Send and purge", no: "Cancel", danger: true });
      if (!ok) return;
    }
    const out = await post("/api/household/send", { ids, dest: dest.key, purge: trashed.length > 0 });
    if (!out) return;
    markSent(out.done);
    report(out, plural(out.done.length, "photo") + " sent to " + dest.label);
  }
  async function trashFlow(ids) {
    if (!ids.length) return;
    const one = ids.length === 1;
    const ok = await ask({
      title: "Move " + (one ? "this photo" : ids.length + " photos") + " to Immich's trash?",
      text: "It goes to the trash of the account it belongs to, which Immich keeps for a set time before deleting it for good. The pipeline's own copies, if any, are not touched.",
      yes: "Move to trash", no: "Cancel", danger: true });
    if (!ok) return;
    const out = await post("/api/household/trash", { ids });
    if (!out) return;
    dropItems(out.done);
    report(out, plural(out.done.length, "photo") + " moved to Immich's trash");
  }
  function download(ids) {
    if (!ids.length) return;
    if (ids.length === 1) {
      const a = document.createElement("a"); a.href = "/api/household/" + ids[0] + "/download"; a.download = "";
      document.body.appendChild(a); a.click(); a.remove(); return;
    }
    let frame = $("ph-dlframe");
    if (!frame) { frame = document.createElement("iframe"); frame.id = "ph-dlframe"; frame.name = "ph-dlframe"; frame.hidden = true; document.body.appendChild(frame); }
    const f = document.createElement("form"); f.method = "post"; f.action = "/api/household/download"; f.target = "ph-dlframe"; f.hidden = true;
    const i = document.createElement("input"); i.type = "hidden"; i.name = "ids"; i.value = ids.join(","); f.appendChild(i);
    document.body.appendChild(f); f.submit(); f.remove();
    toast("Downloading " + ids.length + " photos as one zip.");
  }

  // A small yes/no box over the page; resolves true or false.
  function ask({ title, text, yes, no, danger }) {
    return new Promise(resolve => {
      const back = document.createElement("div"); back.className = "modalback ph-modalback";
      back.innerHTML = '<div class="modal" role="alertdialog" aria-modal="true" aria-labelledby="ph-dt"><h3 id="ph-dt"></h3><p></p>' +
        '<div class="acts"><button class="btn sec" data-no></button><button class="btn' + (danger ? " danger" : "") + '" data-yes></button></div></div>';
      back.querySelector("h3").textContent = title; back.querySelector("p").textContent = text;
      back.querySelector("[data-no]").textContent = no; back.querySelector("[data-yes]").textContent = yes;
      const done = v => { back.remove(); document.removeEventListener("keydown", key, true); resolve(v); };
      const key = e => { if (e.key === "Escape") { e.stopPropagation(); done(false); } };
      back.querySelector("[data-no]").onclick = () => done(false);
      back.querySelector("[data-yes]").onclick = () => done(true);
      back.addEventListener("mousedown", e => { if (e.target === back) done(false); });
      document.addEventListener("keydown", key, true);
      document.body.appendChild(back);
      back.querySelector("[data-no]").focus();
    });
  }
  let toastEl = null, toastTimer = null;
  function toast(text, bad) {
    if (!text) return;
    if (!toastEl) { toastEl = document.createElement("div"); toastEl.className = "ph-toast"; toastEl.setAttribute("role", "status"); document.body.appendChild(toastEl); }
    toastEl.textContent = text; toastEl.classList.toggle("bad", !!bad); toastEl.hidden = false;
    clearTimeout(toastTimer); toastTimer = setTimeout(() => { toastEl.hidden = true; }, 7000);
  }

  // ---- coming and going ----
  function show_(which) {
    if (!built) build();
    if (which === "collage") { enterCollage(); return; }
    leaveCollage();
    if (!started) start(); else { sc.scrollTop = savedTop; schedule(); follow(); }
  }
  function hide() {
    if (built && !pane.hidden && !collage) savedTop = sc.scrollTop;
    closeViewer(); closeSendMenu(); closePanel(); leaveCollage();
  }
  return { show: show_, hide };
})();
