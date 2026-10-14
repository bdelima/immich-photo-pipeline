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

function treeItem(v, label, n, active, base) {
  return `<a class="item${v === active ? " active" : ""}" href="${base}${viewHash(v)}" data-view="${esc(v)}"><span>${esc(label)}</span><span class="count">${n}</span></a>`;
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
