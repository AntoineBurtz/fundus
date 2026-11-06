/* Fundus – Oberfläche ohne Build-Schritt */
(function () {
  "use strict";
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  async function api(method, url, body) {
    const res = await fetch(url, {
      method,
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || data.detail || res.statusText);
    return data;
  }

  // Scrollposition über Neuladen hinweg behalten
  function reloadKeepingScroll(params) {
    sessionStorage.setItem("fundus-scroll", JSON.stringify({ path: location.pathname, y: window.scrollY }));
    const url = new URL(location.href);
    url.hash = "";
    for (const [k, v] of Object.entries(params || {})) {
      if (v === null) url.searchParams.delete(k); else url.searchParams.set(k, v);
    }
    url.searchParams.delete("msg");
    location.replace(url.toString());
  }
  let restoring = false;
  try {
    const saved = JSON.parse(sessionStorage.getItem("fundus-scroll") || "null");
    if (saved && saved.path === location.pathname) {
      sessionStorage.removeItem("fundus-scroll");
      restoring = true;
      window.addEventListener("load", () => window.scrollTo(0, saved.y));
    }
  } catch (e) { /* ignorieren */ }

  // ------------------------------------------------ Tabs (Erfassen, Seitenleiste)
  $$("[data-tabs]").forEach((box) => {
    $$(".adder-tabs button", box).forEach((btn) => btn.addEventListener("click", () => {
      $$(".adder-tabs button", box).forEach((b) => b.classList.toggle("on", b === btn));
      $$(".adder-pane", box).forEach((p) => p.classList.toggle("on", p.dataset.pane === btn.dataset.tab));
      const first = $(".adder-pane.on input:not([type=hidden]), .adder-pane.on textarea", box);
      if (first) first.focus();
    }));
  });
  $$(".side-tabs button").forEach((btn) => btn.addEventListener("click", () => {
    $$(".side-tabs button").forEach((b) => b.classList.toggle("on", b === btn));
    $$(".side-pane").forEach((p) => p.classList.toggle("on", p.dataset.pane === btn.dataset.side));
  }));

  // Drag & Drop-Feld zeigt Dateinamen
  $$(".drop input[type=file]").forEach((inp) => inp.addEventListener("change", () => {
    const span = inp.parentElement.querySelector("span");
    span.textContent = Array.from(inp.files).map((f) => f.name).join(", ") || span.textContent;
  }));

  // Liste aktualisieren, solange etwas verarbeitet wird
  const auto = $("[data-autorefresh]");
  if (auto) setTimeout(() => reloadKeepingScroll(), Number(auto.dataset.autorefresh) * 1000);

  // Zitat kopieren
  $$(".copy-cite").forEach((btn) => btn.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(btn.dataset.cite);
      const t = btn.textContent; btn.textContent = "Kopiert ✓";
      setTimeout(() => (btn.textContent = t), 1500);
    } catch (e) { window.prompt("Zitat:", btn.dataset.cite); }
  }));

  // ------------------------------------------------ Quellenansicht
  const layout = $(".source-layout");
  if (!layout) return;
  const sourceId = Number(layout.dataset.source);
  const reader = $("#reader");
  const player = $("#player");
  const tool = $("#seltool");

  // Status abfragen, solange verarbeitet wird
  if (["wartet", "läuft"].includes(layout.dataset.state)) {
    const poll = async () => {
      try {
        const st = await api("GET", `/s/${sourceId}/state`);
        if (!["wartet", "läuft"].includes(st.state)) return reloadKeepingScroll();
        showJob(st);
      } catch (e) { /* weiter versuchen */ }
      setTimeout(poll, 2500);
    };
    setTimeout(poll, 1500);
  }

  function showJob(st) {
    const note = $("#job-note"), eta = $("#job-eta"), bar = $("#job-bar"), btn = $("#job-cancel");
    if (!note) return;
    if (st.job_status === "running") {
      const pct = st.progress == null ? "" : ` · ${Math.round(st.progress * 100)} %`;
      note.textContent = `${st.note || "Wird verarbeitet"}${pct} …`;
    } else if (st.job === "transcribe") {
      note.textContent = "Wartet, bis die laufende Transkription fertig ist …";
    }
    eta.textContent = st.eta_text || "";
    bar.parentElement.classList.toggle("indet", st.progress == null);
    bar.style.width = `${((st.progress || 0) * 100).toFixed(1)}%`;
    if (st.cancelling && btn) { btn.disabled = true; btn.textContent = "Wird abgebrochen …"; }
  }

  // --- Auswahl → Zeichen-Offsets im Quelltext
  function pointToOffset(node, offset) {
    const el = node.nodeType === 1 ? node : node.parentElement;
    const txt = el && el.closest(".txt");
    if (txt) {
      const base = Number(txt.parentElement.dataset.off);
      const walker = document.createTreeWalker(txt, NodeFilter.SHOW_TEXT);
      let count = 0;
      if (node.nodeType === 3) {
        while (walker.nextNode()) {
          if (walker.currentNode === node) return base + count + offset;
          count += walker.currentNode.nodeValue.length;
        }
        return base + count;
      }
      // Element-Knoten: Text aller Kinder vor "offset" zählen
      const stop = node.childNodes[offset] || null;
      while (walker.nextNode()) {
        if (stop && (stop === walker.currentNode || stop.contains(walker.currentNode))) break;
        count += walker.currentNode.nodeValue.length;
      }
      return base + count;
    }
    const blk = el && el.closest(".blk");
    if (blk) return Number(blk.dataset.off);
    if (node === reader) {
      const child = reader.children[offset];
      if (child) return Number(child.dataset.off);
      const last = reader.lastElementChild;
      return last ? Number(last.dataset.off) + $(".txt", last).textContent.length : 0;
    }
    return null;
  }

  function currentSelection() {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || !reader) return null;
    const range = sel.getRangeAt(0);
    if (!reader.contains(range.commonAncestorContainer)) return null;
    let start = pointToOffset(range.startContainer, range.startOffset);
    let end = pointToOffset(range.endContainer, range.endOffset);
    if (start === null || end === null || end <= start) return null;
    // Leerraum an den Rändern abschneiden
    const raw = sel.toString();
    const lead = raw.length - raw.replace(/^\s+/, "").length;
    const trail = raw.length - raw.replace(/\s+$/, "").length;
    start += Math.min(lead, end - start);
    end -= Math.min(trail, end - start);
    if (end <= start) return null;
    return { start, end, rect: range.getBoundingClientRect() };
  }

  let pending = null;
  function hideTool() { tool.hidden = true; pending = null; }
  if (reader && tool) {
    const showTool = () => {
        const s = currentSelection();
        if (!s) return hideTool();
        pending = s;
        tool.hidden = false;
        const top = window.scrollY + s.rect.top - tool.offsetHeight - 10;
        const left = Math.max(8, Math.min(window.scrollX + s.rect.left + s.rect.width / 2 - tool.offsetWidth / 2,
          window.scrollX + document.documentElement.clientWidth - tool.offsetWidth - 8));
        tool.style.top = `${Math.max(window.scrollY + 8, top)}px`;
        tool.style.left = `${left}px`;
    };
    document.addEventListener("mouseup", (ev) => {
      if (tool.contains(ev.target)) return;
      setTimeout(showTool, 10);
    });
    // Touch-Geräte: Auswahl erfolgt über Ziehpunkte, kein mouseup
    let selTimer = null;
    document.addEventListener("selectionchange", () => {
      if (!matchMedia("(pointer: coarse)").matches) return;
      clearTimeout(selTimer);
      selTimer = setTimeout(showTool, 450);
    });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") hideTool(); });
    $$("button[data-color]", tool).forEach((btn) => btn.addEventListener("click", async () => {
      if (!pending) return;
      const { start, end } = pending;
      hideTool();
      try {
        const res = await api("POST", "/api/highlights", { source_id: sourceId, start, end, color: btn.dataset.color });
        reloadKeepingScroll({ h: res.id, edit: 1 });
      } catch (e) { window.alert("Markieren fehlgeschlagen: " + e.message); }
    }));
  }

  // --- Markierungen: Klick im Text ↔ Karte in der Seitenleiste
  function focusCard(id, openEdit) {
    const card = document.getElementById(`hl-${id}`);
    if (!card) return;
    $$(".side-tabs button").forEach((b) => b.classList.toggle("on", b.dataset.side === "hl"));
    $$(".side-pane").forEach((p) => p.classList.toggle("on", p.dataset.pane === "hl"));
    $$(".hl-item.focus").forEach((c) => c.classList.remove("focus"));
    card.classList.add("focus");
    card.scrollIntoView({ block: "nearest", behavior: "smooth" });
    if (openEdit) {
      const d = $(".hl-edit", card); d.open = true;
      const ta = $("textarea", d); if (ta) setTimeout(() => ta.focus(), 50);
    }
    $$("mark.hl").forEach((m) => m.classList.toggle("active", m.dataset.ids.split(",").includes(String(id))));
  }
  function scrollToMark(id) {
    const mark = $$("mark.hl").find((m) => m.dataset.ids.split(",").includes(String(id)));
    if (mark) mark.scrollIntoView({ block: "center", behavior: "smooth" });
  }
  if (reader) reader.addEventListener("click", (e) => {
    const mark = e.target.closest("mark.hl");
    if (mark && window.getSelection().isCollapsed) {
      const ids = mark.dataset.ids.split(",");
      focusCard(ids[ids.length - 1]);
    }
  });
  $$(".hl-item .jump").forEach((q) => q.addEventListener("click", () => {
    const id = q.closest(".hl-item").dataset.id;
    focusCard(id); scrollToMark(id);
  }));

  // Bearbeiten / Löschen
  $$("form[data-hl-form]").forEach((form) => form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(form);
    try {
      await api("PATCH", `/api/highlights/${form.dataset.hlForm}`, {
        note: fd.get("note"), color: fd.get("color"), tags: fd.get("tags"), event_date: fd.get("event_date") || null,
      });
      reloadKeepingScroll({ h: form.dataset.hlForm, edit: null });
    } catch (err) { window.alert(err.message); }
  }));
  $$("[data-hl-delete]").forEach((btn) => btn.addEventListener("click", async () => {
    if (!window.confirm("Markierung löschen?")) return;
    await api("DELETE", `/api/highlights/${btn.dataset.hlDelete}`);
    reloadKeepingScroll({ h: null, edit: null });
  }));

  // --- Player ↔ Transkript
  function seek(t) {
    if (!player) return;
    player.currentTime = Number(t);
    player.play().catch(() => {});
  }
  $$(".blk .tc").forEach((b) => b.addEventListener("click", () => seek(b.parentElement.dataset.t)));
  $$(".tc-link").forEach((b) => b.addEventListener("click", () => seek(b.dataset.t)));

  if (player && reader) {
    const blocks = $$(".blk[data-t]", reader);
    const times = blocks.map((b) => Number(b.dataset.t));
    const follow = $("#follow");
    let active = null;
    player.addEventListener("timeupdate", () => {
      const t = player.currentTime;
      let lo = 0, hi = times.length - 1, idx = -1;
      while (lo <= hi) { const mid = (lo + hi) >> 1; if (times[mid] <= t) { idx = mid; lo = mid + 1; } else hi = mid - 1; }
      const blk = idx >= 0 ? blocks[idx] : null;
      if (blk === active) return;
      if (active) active.classList.remove("now");
      active = blk;
      if (blk) {
        blk.classList.add("now");
        if (follow && follow.checked && !player.paused) blk.scrollIntoView({ block: "center", behavior: "smooth" });
      }
    });
  }

  // Einstieg über Suchtreffer / Zeitstrahl: ?t=…&h=…
  const params = new URLSearchParams(location.search);
  if (layout.dataset.start && player) {
    const t = Number(layout.dataset.start);
    const go = () => { player.currentTime = t; };
    if (player.readyState >= 1) go(); else player.addEventListener("loadedmetadata", go, { once: true });
    const blk = $$(".blk[data-t]").filter((b) => Number(b.dataset.t) <= t).pop();
    if (blk && !layout.dataset.focus) { blk.classList.add("now"); setTimeout(() => blk.scrollIntoView({ block: "center" }), 100); }
  }
  if (layout.dataset.focus) {
    const id = layout.dataset.focus;
    setTimeout(() => {
      focusCard(id, params.get("edit") === "1");
      if (!restoring) scrollToMark(id);
    }, 120);
  }
})();
