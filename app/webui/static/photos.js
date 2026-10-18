// Household Photos: a read-only, scrolling timeline of everyone's photos (those who partnered the
// pipeline account in Immich), with a full-window viewer. Pictures come through the pipeline's own
// proxy routes, never straight from Immich.
(function () {
  const ROW = 200, GAP = 4, NEAR = 1800;
  const main = document.getElementById("ph-main");
  const host = document.getElementById("ph-months");
  const rail = document.getElementById("ph-rail");
  const note = document.getElementById("ph-note");
  const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
  const secs = {};          // bucket -> section state
  let order = [];           // buckets, newest first
  let flat = [];            // loaded photos in display order, for the viewer
  let cur = -1;

  function monthLabel(m) { const [y, mo] = m.split("-"); return MONTHS[+mo - 1] + " " + y; }
  function dur(s) { s = Math.round(s); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), r = s % 60;
    return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(r).padStart(2, "0"); }
  function niceBytes(n) { return n >= 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1024)) + " KB"; }
  function dayLabel(d) { const dt = new Date(d + "T12:00:00"); return dt.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric", year: "numeric" }); }

  function showNote(html) { note.innerHTML = html; note.hidden = !html; }
  const HELP = "Each household member needs to add the pipeline account as a partner in Immich " +
    "(Account settings &rarr; Sharing &rarr; Partners). Their photos then appear here.";

  async function start() {
    let data;
    try { data = await api("/api/household/months"); }
    catch (e) { showNote("<b>Couldn't read the timeline from Immich.</b> " + esc(e.message || String(e))); return; }
    order = data.months.map(m => m.bucket);
    const total = data.months.reduce((n, m) => n + m.count, 0);
    document.getElementById("ph-count").textContent = total ? "about " + total.toLocaleString() + " photos" : "";
    if (!order.length) { showNote("<b>Nothing to show yet.</b> " + HELP); return; }
    for (const m of data.months) {
      const el = document.createElement("section");
      el.className = "ph-month"; el.dataset.bucket = m.bucket;
      el.innerHTML = '<h3 class="ph-mh">' + esc(monthLabel(m.month)) + ' <span class="muted">' + m.count.toLocaleString() + '</span></h3><div class="ph-body"></div>';
      const body = el.querySelector(".ph-body");
      body.style.minHeight = Math.max(80, Math.ceil(m.count / 6) * (ROW + GAP)) + "px";
      host.appendChild(el);
      secs[m.bucket] = { el, body, month: m.month, count: m.count, state: "idle", items: [] };
    }
    buildRail(data.months);
    main.addEventListener("scroll", schedule, { passive: true });
    window.addEventListener("resize", () => { for (const b of order) if (secs[b].state === "loaded") layout(secs[b]); schedule(); });
    schedule();
  }

  function buildRail(months) {
    const years = [];
    for (const m of months) { const y = m.month.slice(0, 4); if (!years.length || years[years.length - 1].y !== y) years.push({ y, first: m.bucket, n: 0 }); years[years.length - 1].n += m.count; }
    rail.innerHTML = years.map(y => '<button data-b="' + esc(y.first) + '">' + y.y + '</button>').join("");
    rail.onclick = e => { const b = e.target.closest("button"); if (b) secs[b.dataset.b].el.scrollIntoView({ block: "start" }); };
  }

  let tick = 0;
  function schedule() { if (!tick) tick = requestAnimationFrame(() => { tick = 0; update(); }); }
  function update() {
    const box = main.getBoundingClientRect();
    let topYear = null;
    for (const b of order) {
      const s = secs[b], r = s.el.getBoundingClientRect();
      const near = r.bottom > box.top - NEAR && r.top < box.bottom + NEAR;
      if (near && s.state === "idle") load(s);
      else if (!near && s.state === "loaded") unload(s);
      if (topYear === null && r.bottom > box.top + 40) topYear = s.month.slice(0, 4);
    }
    for (const x of rail.children) x.classList.toggle("on", x.textContent === topYear);
  }

  async function load(s) {
    s.state = "loading";
    try { s.items = (await api("/api/household/month?bucket=" + encodeURIComponent(s.el.dataset.bucket))).items; }
    catch (e) { s.state = "idle"; s.body.innerHTML = '<p class="muted">Couldn\'t load this month. ' + esc(e.message || "") + "</p>"; s.failed = true; return; }
    s.state = "loaded"; layout(s); reflat();
  }
  function unload(s) {
    s.body.style.minHeight = s.body.offsetHeight + "px";
    s.body.innerHTML = ""; s.state = "idle"; reflat();
  }
  function reflat() { flat = []; for (const b of order) if (secs[b].state === "loaded") flat.push(...secs[b].items); }

  function layout(s) {
    const W = s.body.clientWidth || main.clientWidth;
    s.body.innerHTML = ""; s.body.style.minHeight = "";
    if (!s.items.length) { s.body.innerHTML = '<p class="muted">Nothing here.</p>'; return; }
    const days = [];
    for (const it of s.items) { const d = days[days.length - 1]; if (d && d.day === it.day) d.items.push(it); else days.push({ day: it.day, items: [it] }); }
    for (const d of days) {
      const h = document.createElement("div"); h.className = "ph-day"; h.textContent = dayLabel(d.day); s.body.appendChild(h);
      let row = [], sum = 0;
      const flush = last => {
        if (!row.length) return;
        const h = last ? ROW : Math.min((W - GAP * (row.length - 1)) / sum, ROW * 1.6);
        const el = document.createElement("div"); el.className = "ph-row";
        for (const it of row) el.appendChild(tile(it, h * it.ratio, h));
        s.body.appendChild(el); row = []; sum = 0;
      };
      for (const it of d.items) {
        row.push(it); sum += it.ratio;
        if ((W - GAP * (row.length - 1)) / sum <= ROW) flush(false);
      }
      flush(true);
    }
  }

  function tile(it, w, h) {
    const a = document.createElement("button");
    a.className = "ph-tile"; a.style.width = w + "px"; a.style.height = h + "px";
    a.setAttribute("aria-label", (it.video ? "Video" : "Photo") + (it.owner ? " from " + it.owner : "") + ", " + it.day);
    const img = document.createElement("img"); img.loading = "lazy"; img.alt = ""; img.src = "/api/household/" + it.id + "/thumb";
    a.appendChild(img);
    if (it.owner) { const o = document.createElement("span"); o.className = "ph-owner"; o.textContent = it.owner; a.appendChild(o); }
    if (it.video) { const v = document.createElement("span"); v.className = "ph-dur"; v.textContent = "▶ " + dur(it.seconds || 0); a.appendChild(v); }
    a.onclick = () => openViewer(it.id);
    return a;
  }

  // ---- viewer ----
  const pv = document.getElementById("pv"), stage = document.getElementById("pv-stage"),
    side = document.getElementById("pv-side"), title = document.getElementById("pv-title");
  let zoomed = false, infoOn = false;

  function openViewer(id) { cur = flat.findIndex(x => x.id === id); if (cur < 0) return; pv.hidden = false; show(); }
  function closeViewer() { pv.hidden = true; stage.innerHTML = ""; }
  function show() {
    const it = flat[cur]; zoomed = false;
    title.textContent = dayLabel(it.day) + (it.time ? " · " + it.time : "") + (it.owner ? " · " + it.owner : "");
    stage.className = "pv-stage";
    stage.innerHTML = it.video
      ? '<video controls autoplay playsinline src="/api/household/' + it.id + '/video"></video>'
      : '<img alt="" src="/api/household/' + it.id + '/preview">';
    document.getElementById("pv-zoom").disabled = it.video;
    document.getElementById("pv-prev").hidden = cur <= 0;
    document.getElementById("pv-next").hidden = cur >= flat.length - 1;
    if (infoOn) loadInfo(it);
  }
  function step(d) { const n = cur + d; if (n >= 0 && n < flat.length) { cur = n; show(); } }
  async function loadInfo(it) {
    side.innerHTML = '<p class="muted">Loading&hellip;</p>';
    try {
      const r = await api("/api/household/" + it.id), m = r.meta || {}, rows = [];
      const add = (k, v) => { if (v) rows.push("<dt>" + k + "</dt><dd>" + esc(String(v)) + "</dd>"); };
      const size = [m.width && m.height ? m.width + " × " + m.height : "", m.size_bytes ? niceBytes(m.size_bytes) : ""].filter(Boolean).join(" · ");
      const exposure = [m.exposure_time ? m.exposure_time + " s" : "", m.f_number ? "f/" + m.f_number : "", m.iso ? "ISO " + m.iso : ""].filter(Boolean).join(" · ");
      add("File", r.file_name); add("Owner", m.owner_name); add("Taken", m.taken); add("Place", m.place);
      add("Camera", m.camera); add("Exposure", exposure); add("Size", size);
      if (flat[cur] === it) side.innerHTML = "<h4>Details</h4><dl>" + (rows.join("") || "<dd>No details.</dd>") + "</dl>";
    } catch (e) { side.innerHTML = '<p class="muted">Couldn\'t load details.</p>'; }
  }
  document.getElementById("pv-back").onclick = closeViewer;
  document.getElementById("pv-prev").onclick = () => step(-1);
  document.getElementById("pv-next").onclick = () => step(1);
  document.getElementById("pv-zoom").onclick = () => { zoomed = !zoomed; stage.classList.toggle("zoomed", zoomed); };
  document.getElementById("pv-info").onclick = () => { infoOn = !infoOn; side.hidden = !infoOn; if (infoOn) loadInfo(flat[cur]); };
  document.addEventListener("keydown", e => {
    if (pv.hidden) return;
    if (e.key === "Escape") closeViewer(); else if (e.key === "ArrowLeft") step(-1); else if (e.key === "ArrowRight") step(1);
  });

  start();
})();
