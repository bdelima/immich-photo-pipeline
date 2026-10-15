// Pieces shared by every page: small helpers and the sidebar.
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const api = async url => { const r = await fetch(url); if (!r.ok) throw new Error(url + " " + r.status); return r.json(); };

// A view is "review", "live", "queue", "trash" or "album:<slug>". It is the
// address fragment of the main page (/#album:costa-rica), which is how the
// notes on the Immich albums link straight to one. An older link that has
// the album's name (/#album:Costa%20Rica) still works.
const viewHash = v => "#" + encodeURIComponent(v).replace(/%3A/gi, ":");
const hashView = h => { try { return decodeURIComponent((h || "").replace(/^#/, "")); } catch (e) { return ""; } };
const viewTitle = (v, albums) => {
  if (!v.startsWith("album:")) return v.charAt(0).toUpperCase() + v.slice(1);
  const a = (albums || []).find(x => x.view === v || x.name === v.slice(6));
  return a ? a.name : v.slice(6);
};
// The canonical view for an address: an old name link becomes its slug link.
const canonicalView = (v, albums) => {
  if (!v.startsWith("album:")) return v;
  const a = (albums || []).find(x => x.view === v || x.name === v.slice(6));
  return a ? a.view : v;
};

// ---- the counts in the sidebar ----
// A count that changes while the page is open is marked: its number goes bold and a small
// badge says by how much (green for more, amber for fewer). The mark stays until that row is
// clicked; the one exception is a count that has dropped to 0, whose mark is removed after a
// few seconds, since there is nothing left to look at. The first count seen is not a change.
// Queue, Review and Trash also put the count in a blue pill while it is above zero.
const PILL_VIEWS = ["queue", "review", "trash"];
const ZERO_CLEAR_MS = 10000;
const countState = {};          // view -> { n, delta, changed }
const zeroTimers = {};
function noteCount(v, n) {
  const s = countState[v];
  if (!s) return (countState[v] = { n, delta: 0, changed: false });
  if (n !== s.n) {
    s.delta += n - s.n; s.n = n; s.changed = true;
    clearTimeout(zeroTimers[v]);
    if (n === 0) zeroTimers[v] = setTimeout(() => clearCountMark(v), ZERO_CLEAR_MS);
  }
  return s;
}
function countHtml(v, n) {
  const s = noteCount(v, n);
  const badge = s.delta ? `<span class="cdelta ${s.delta > 0 ? "up" : "down"}" title="Changed by ${s.delta > 0 ? "+" : "−"}${Math.abs(s.delta)} since you last looked">${s.delta > 0 ? "+" : "−"}${Math.abs(s.delta)}</span>` : "";
  const pill = PILL_VIEWS.includes(v) && n > 0 ? " pill" : "";
  return `<span class="count${s.changed ? " changed" : ""}">${badge}<span class="n${pill}">${n}</span></span>`;
}
// Brings one row's count up to date without redrawing the sidebar.
function paintCount(a, v, n) {
  const was = a.querySelector(".count"), html = countHtml(v, n);
  if (was && was.outerHTML !== html) was.outerHTML = html;
}
function clearCountMark(v) {
  clearTimeout(zeroTimers[v]);
  const s = countState[v];
  if (!s || (!s.delta && !s.changed)) return;
  s.delta = 0; s.changed = false;
  const a = document.querySelector(`#tree a.item[data-view="${CSS.escape(v)}"]`);
  if (a) paintCount(a, v, s.n);
}
// Clicking a row is looking at it: its mark goes.
document.addEventListener("click", e => {
  const a = e.target.closest && e.target.closest("#tree a.item");
  if (a) clearCountMark(a.dataset.view);
});

function treeItem(v, label, n, active, base) {
  return `<a class="item${v === active ? " active" : ""}" href="${base}${viewHash(v)}" data-view="${esc(v)}"><span>${esc(label)}</span>${countHtml(v, n)}</a>`;
}
// Draws the sidebar. `base` is "" on the main page and "/" elsewhere, so the
// same links work from the Recipe rules page.
async function renderTree(active, base) {
  const t = await api("/api/tree");
  $("tree").innerHTML =
    treeItem("queue", "Queue", t.queue, active, base) + treeItem("review", "Review", t.review, active, base) +
    treeItem("live", "Live", t.live, active, base) +
    `<div class="group">Albums</div>` + t.albums.map(a => treeItem(a.view, a.name, a.count, active, base)).join("") +
    `<div class="group">&nbsp;</div>` + treeItem("trash", "Trash", t.trash, active, base);
  return t;
}

// A small version hint under the title, so it is clear which build is running.
(async () => {
  const h = document.querySelector("nav h1");
  if (!h) return;
  try {
    const v = (await api("/api/version")).version;
    const el = document.createElement("div");
    el.className = "ver";
    el.textContent = /^\d/.test(v) ? "v" + v : v;
    (h.closest(".brand") || h).after(el);       // under the icon and title, not inside the row
  } catch (e) { /* the hint is optional */ }
})();
