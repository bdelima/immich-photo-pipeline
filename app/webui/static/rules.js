// The Recipe rules view of the main page (#rules). The server draws the cards
// (/api/rules/view); this wires them up. An action redraws the view in place.
const RulesPane = (() => {
  const pane = () => $("rulesView");
  const maxChars = () => +pane().querySelector(".rules-page").dataset.maxChars;
  const say = text => { const s = $("rulesStatus"); if (s) s.textContent = text; };

  async function call(url, body) {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await res.json().catch(() => ({}));
    if (res.ok) await show(); else say("Error: " + (data.error || res.status));
  }

  // A text box that grows with what is typed, with a count of characters used.
  function wireBox(box, counter, max) {
    const grow = () => {
      box.style.height = "auto";
      box.style.height = Math.max(88, Math.min(box.scrollHeight + 2, 320)) + "px";
      counter.textContent = box.value.length + " / " + max;
      counter.className = "counter" + (box.value.length >= max ? " over" : "");
    };
    box.addEventListener("input", grow);
    grow();
    return grow;
  }

  function wire() {
    const root = pane(), max = maxChars();
    root.querySelectorAll("button[data-act]").forEach(b =>
      b.addEventListener("click", () => call(`/api/rules/${b.dataset.id}/${b.dataset.act}`)));
    root.querySelectorAll(".rule[data-rule]").forEach(card => {
      const editor = card.querySelector(".editor"), box = card.querySelector("[data-text]");
      const grow = wireBox(box, card.querySelector(".counter"), max);
      const edit = editing => {
        editor.hidden = !editing;
        card.querySelectorAll(".rule-view").forEach(el => { el.hidden = editing; });
        if (editing) { grow(); box.focus(); box.setSelectionRange(box.value.length, box.value.length); }
      };
      card.querySelector("[data-edit]").addEventListener("click", () => edit(true));
      card.querySelector("[data-cancel]").addEventListener("click", () => { box.value = box.defaultValue; edit(false); });
      card.querySelector("[data-save]").addEventListener("click", () =>
        call(`/api/rules/${card.dataset.rule}/edit`, { text: box.value, scope: card.querySelector("[data-scope]").value }));
    });
    const form = $("add-form");
    wireBox(form.querySelector("textarea"), form.querySelector(".counter"), max);
    form.addEventListener("submit", e => {
      e.preventDefault();
      const f = new FormData(form);
      call("/api/rules", { text: f.get("text"), scope: f.get("scope") });
    });
  }

  // Draws the view afresh from the server: when it is opened, and after every change.
  async function show() {
    try {
      const res = await fetch("/api/rules/view");
      if (!res.ok) throw new Error(res.status);
      pane().innerHTML = await res.text();
    } catch (e) {
      pane().innerHTML = '<div class="rules-page"><p class="muted">Couldn\'t load the rules.</p></div>';
      return;
    }
    wire();
  }
  return { show };
})();
