// The AOI editor of the video page (PLAN §9.3). Served from /admin/static because the proxy's
// content security policy allows scripts from the same origin only (no inline scripts).
document.addEventListener("DOMContentLoaded", () => {
  const root = document.getElementById("aoi-editor");
  const textarea = document.querySelector('textarea[name="aois"]');
  const stage = root.querySelector(".stage");
  const video = stage.querySelector("video");
  const svg = stage.querySelector("svg");
  const slider = document.getElementById("aoi-time");
  const clock = document.getElementById("aoi-clock");
  const notice = document.getElementById("aoi-notice");
  const hint = document.getElementById("aoi-hint");
  const finishButton = document.getElementById("aoi-finish");
  const cancelButton = document.getElementById("aoi-cancel");
  const NS = "http://www.w3.org/2000/svg";
  const duration = Number(root.dataset.duration);
  const MIN_SIZE = 0.01;
  const HINTS = {
    rect: "Drag on the picture to draw the rectangle.",
    polygon: "Click the corners; double-click or Finish to close the shape.",
  };

  let data = { segments: [], aois: [] };
  let paused = false;
  let selected = null; // index in data.aois
  let mode = null; // "rect" | "polygon" | null
  let draft = []; // corners of the polygon being drawn
  let hover = null;
  let drag = null;

  const clamp = (v) => Math.min(1, Math.max(0, v));
  const round = (v, digits) => Math.round(v * 10 ** digits) / 10 ** digits;
  const finite = (v) => typeof v === "number" && Number.isFinite(v);

  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") node.className = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v);
    }
    node.append(...children);
    return node;
  }
  function svgEl(tag, attrs = {}) {
    const node = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, String(v));
    return node;
  }

  // ---- text <-> data -------------------------------------------------------------------
  function fail(message) {
    paused = true;
    root.classList.add("paused");
    notice.textContent = message + " The editor is paused until the text is valid.";
    notice.hidden = false;
  }

  function readText() {
    const text = textarea.value.trim();
    let value = {};
    if (text !== "") {
      try { value = JSON.parse(text); } catch (e) { return fail("The text is not valid JSON (" + e.message + ")."); }
      if (value === null || typeof value !== "object" || Array.isArray(value)) {
        return fail("The text must be a JSON object.");
      }
    }
    const aois = [];
    for (const a of value.aois ?? []) {
      const coordsOk = a.shape === "rect"
        ? Array.isArray(a.coords) && a.coords.length === 4 && a.coords.every(finite)
        : a.shape === "polygon" && Array.isArray(a.coords) && a.coords.length >= 3
          && a.coords.every((p) => Array.isArray(p) && p.length === 2 && p.every(finite));
      if (!coordsOk || !finite(a.start) || !finite(a.end)) {
        return fail("An area in the text has a shape the editor cannot show.");
      }
      aois.push({ id: String(a.id ?? ""), label: String(a.label ?? ""), start: a.start, end: a.end,
                  shape: a.shape, coords: a.coords });
    }
    const segments = [];
    for (const s of value.segments ?? []) {
      if (!finite(s.start) || !finite(s.end)) return fail("A segment in the text has no valid times.");
      segments.push({ id: String(s.id ?? ""), start: s.start, end: s.end });
    }
    data = { segments, aois };
    paused = false;
    root.classList.remove("paused");
    notice.hidden = true;
    if (selected !== null && selected >= aois.length) selected = null;
    renderTables();
    draw();
  }

  function writeText() {
    textarea.value = data.segments.length || data.aois.length ? JSON.stringify(data, null, 2) : "";
    showProblems();
  }

  function showProblems() {
    const list = document.getElementById("aoi-problems");
    list.replaceChildren();
    const problems = [];
    for (const [name, items] of [["area", data.aois], ["segment", data.segments]]) {
      const ids = new Set();
      items.forEach((it, i) => {
        if (it.id === "") problems.push(`A ${name} has no id.`);
        else if (ids.has(it.id)) problems.push(`Two ${name}s have the id "${it.id}".`);
        ids.add(it.id);
        if (!(it.end > it.start)) problems.push(`The ${name} "${it.id || i + 1}" must end after it starts.`);
        if (it.start < 0) problems.push(`The ${name} "${it.id || i + 1}" starts before 0.`);
        if (it.shape === "rect" && !(it.coords[2] > it.coords[0] && it.coords[3] > it.coords[1])) {
          problems.push(`The ${name} "${it.id || i + 1}" is a rectangle with no size: coords must be [x0, y0, x1, y1] with x1 > x0 and y1 > y0.`);
        }
      });
    }
    for (const p of problems) list.append(el("li", {}, p));
  }

  // ---- geometry --------------------------------------------------------------------------
  const corners = (c) => [[c[0], c[1]], [c[2], c[1]], [c[2], c[3]], [c[0], c[3]]];
  const points = (a) => (a.shape === "rect" ? corners(a.coords) : a.coords);
  const boxOf = (pts) => ({
    x0: Math.min(...pts.map((p) => p[0])), x1: Math.max(...pts.map((p) => p[0])),
    y0: Math.min(...pts.map((p) => p[1])), y1: Math.max(...pts.map((p) => p[1])),
  });
  const active = (a) => a.start <= video.currentTime && video.currentTime < a.end;
  const rectFrom = (p, q) => [round(Math.min(p[0], q[0]), 4), round(Math.min(p[1], q[1]), 4),
                              round(Math.max(p[0], q[0]), 4), round(Math.max(p[1], q[1]), 4)];

  function pointer(e) {
    const r = svg.getBoundingClientRect();
    return [clamp((e.clientX - r.left) / r.width), clamp((e.clientY - r.top) / r.height)];
  }

  // ---- drawing ---------------------------------------------------------------------------
  function draw() {
    const W = stage.clientWidth, H = stage.clientHeight;
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.replaceChildren();
    const px = (p) => `${p[0] * W},${p[1] * H}`;
    data.aois.forEach((a, i) => {
      if (a.shape === "rect" && !(a.coords[2] > a.coords[0] && a.coords[3] > a.coords[1])) return;
      const cls = "aoi" + (active(a) ? "" : " inactive") + (i === selected ? " selected" : "");
      const pts = points(a);
      const shape = a.shape === "rect"
        ? svgEl("rect", { x: a.coords[0] * W, y: a.coords[1] * H,
                          width: (a.coords[2] - a.coords[0]) * W, height: (a.coords[3] - a.coords[1]) * H })
        : svgEl("polygon", { points: pts.map(px).join(" ") });
      shape.setAttribute("class", cls);
      shape.dataset.aoi = String(i);
      svg.append(shape);
      const b = boxOf(pts);
      const label = svgEl("text", { x: b.x0 * W + 4, y: b.y0 * H + 15 });
      label.textContent = a.label || a.id;
      svg.append(label);
    });
    if (selected !== null && data.aois[selected]) {
      points(data.aois[selected]).forEach((p, i) => {
        const h = svgEl("circle", { cx: p[0] * W, cy: p[1] * H, r: 6 });
        h.setAttribute("class", "handle");
        h.dataset.handle = String(i);
        svg.append(h);
      });
    }
    if (drag?.kind === "draw") {
      const [x0, y0, x1, y1] = rectFrom(drag.start, drag.now);
      const r = svgEl("rect", { x: x0 * W, y: y0 * H, width: (x1 - x0) * W, height: (y1 - y0) * H });
      r.setAttribute("class", "draft");
      svg.append(r);
    }
    if (mode === "polygon" && draft.length) {
      const shown = hover ? [...draft, hover] : draft;
      const line = svgEl("polygon", { points: shown.map(px).join(" ") });
      line.setAttribute("class", "draft");
      svg.append(line);
      for (const p of draft) {
        const h = svgEl("circle", { cx: p[0] * W, cy: p[1] * H, r: 4 });
        h.setAttribute("class", "handle");
        svg.append(h);
      }
    }
  }

  // ---- editing ---------------------------------------------------------------------------
  function nextName(prefix, items) {
    let n = 1;
    while (items.some((it) => it.id === `${prefix}_${n}`)) n += 1;
    return n;
  }

  function addAoi(shape, coords) {
    const n = nextName("area", data.aois);
    data.aois.push({ id: `area_${n}`, label: `Area ${n}`, start: 0, end: round(duration, 2), shape, coords });
    selected = data.aois.length - 1;
    writeText();
    renderTables();
  }

  function setMode(next) {
    mode = next;
    draft = [];
    hover = null;
    drag = null;
    root.classList.toggle("drawing", mode !== null);
    for (const b of root.querySelectorAll("[data-mode]")) b.classList.toggle("on", b.dataset.mode === mode);
    hint.textContent = mode ? HINTS[mode] : "";
    finishButton.hidden = mode !== "polygon";
    cancelButton.hidden = mode === null;
    draw();
  }

  function finishPolygon() {
    const last = draft.length - 1;
    if (last > 0 && draft[last][0] === draft[last - 1][0] && draft[last][1] === draft[last - 1][1]) {
      draft.pop(); // the double-click added its point twice (at exactly the same place)
    }
    if (draft.length < 3) return;
    const coords = draft.map((p) => [round(p[0], 4), round(p[1], 4)]);
    setMode(null);
    addAoi("polygon", coords);
    draw();
  }

  svg.addEventListener("pointerdown", (e) => {
    if (paused || e.button !== 0) return; // the main button only
    const p = pointer(e);
    if (mode === "rect") {
      drag = { kind: "draw", start: p, now: p };
      svg.setPointerCapture(e.pointerId);
      return;
    }
    if (mode === "polygon") {
      draft.push(p);
      draw();
      return;
    }
    const t = e.target;
    if (t.dataset.handle !== undefined && selected !== null) {
      drag = { kind: "handle", index: Number(t.dataset.handle), orig: structuredClone(data.aois[selected]) };
      svg.setPointerCapture(e.pointerId);
    } else if (t.dataset.aoi !== undefined) {
      select(Number(t.dataset.aoi));
      drag = { kind: "move", start: p, orig: structuredClone(data.aois[selected]) };
      svg.setPointerCapture(e.pointerId);
    } else {
      select(null);
    }
  });

  svg.addEventListener("pointermove", (e) => {
    const p = pointer(e);
    if (mode === "polygon") { hover = p; draw(); return; }
    if (!drag) return;
    if (drag.kind === "draw") {
      drag.now = p;
    } else if (drag.kind === "move") {
      const orig = drag.orig;
      const b = boxOf(points(orig));
      const dx = Math.min(1 - b.x1, Math.max(-b.x0, p[0] - drag.start[0]));
      const dy = Math.min(1 - b.y1, Math.max(-b.y0, p[1] - drag.start[1]));
      const a = data.aois[selected];
      a.coords = orig.shape === "rect"
        ? [orig.coords[0] + dx, orig.coords[1] + dy, orig.coords[2] + dx, orig.coords[3] + dy].map((v) => round(v, 4))
        : orig.coords.map((c) => [round(c[0] + dx, 4), round(c[1] + dy, 4)]);
    } else if (drag.kind === "handle") {
      const a = data.aois[selected];
      if (a.shape === "rect") {
        const opposite = corners(drag.orig.coords)[(drag.index + 2) % 4];
        a.coords = rectFrom(p, opposite);
      } else {
        a.coords = a.coords.map((c, i) => (i === drag.index ? [round(p[0], 4), round(p[1], 4)] : c));
      }
    }
    draw();
  });

  function endDrag() {
    if (!drag) return;
    const finished = drag;
    drag = null;
    if (finished.kind === "draw") {
      const [x0, y0, x1, y1] = rectFrom(finished.start, finished.now);
      setMode(null);
      if (x1 - x0 >= MIN_SIZE && y1 - y0 >= MIN_SIZE) addAoi("rect", [x0, y0, x1, y1]);
      return;
    }
    const a = data.aois[selected];
    if (a.shape === "rect" && (a.coords[2] - a.coords[0] < MIN_SIZE || a.coords[3] - a.coords[1] < MIN_SIZE)) {
      a.coords = finished.orig.coords; // collapsed to nothing: undo
    }
    writeText();
    draw();
  }
  svg.addEventListener("pointerup", endDrag);
  svg.addEventListener("pointercancel", endDrag);
  svg.addEventListener("dblclick", () => { if (mode === "polygon") finishPolygon(); });

  function select(index) {
    selected = index;
    for (const row of document.querySelectorAll("#aoi-table tr[data-index]")) {
      row.classList.toggle("selected", Number(row.dataset.index) === selected);
    }
    draw();
  }

  // ---- tables ----------------------------------------------------------------------------
  const textField = (item, key, label) => el("input", {
    value: item[key], "aria-label": label,
    oninput: (e) => { if (paused) return; item[key] = e.target.value; writeText(); draw(); },
  });

  function timeField(item, key, label) {
    const input = el("input", { type: "number", step: "0.01", min: "0", value: String(item[key]), "aria-label": label });
    input.addEventListener("input", () => {
      if (paused || input.value === "") return; // paused: the text is not valid; empty: being edited
      item[key] = Number(input.value);
      writeText();
      draw();
    });
    const now = el("button", { type: "button", title: "Use the time of the slider", onclick: () => {
      if (paused) return;
      item[key] = round(video.currentTime, 2);
      input.value = String(item[key]);
      writeText();
      draw();
    } }, "now");
    return el("td", { class: "n" }, input, now);
  }

  function renderTables() {
    const areas = document.getElementById("aoi-table");
    areas.replaceChildren(el("tr", {}, ...["Id", "Label", "Starts (s)", "Ends (s)", "Shape", ""].map((h) => el("th", {}, h))));
    data.aois.forEach((a, i) => {
      const row = el("tr", { "data-index": String(i), class: i === selected ? "selected" : "",
                             onclick: (e) => { if (e.target.tagName === "TD") select(i); } },
        el("td", {}, textField(a, "id", "Area id")), el("td", {}, textField(a, "label", "Area label")),
        timeField(a, "start", "Area starts"), timeField(a, "end", "Area ends"),
        el("td", {}, a.shape),
        el("td", {}, el("button", { type: "button", class: "danger", onclick: () => {
          if (paused) return;
          data.aois.splice(i, 1);
          selected = null;
          writeText(); renderTables(); draw();
        } }, "Delete")));
      areas.append(row);
    });
    if (!data.aois.length) areas.append(el("tr", {}, el("td", { colspan: "6", class: "muted" }, "No areas yet.")));

    const segments = document.getElementById("segment-table");
    segments.replaceChildren(el("tr", {}, ...["Id", "Starts (s)", "Ends (s)", ""].map((h) => el("th", {}, h))));
    data.segments.forEach((s, i) => {
      segments.append(el("tr", {},
        el("td", {}, textField(s, "id", "Segment id")),
        timeField(s, "start", "Segment starts"), timeField(s, "end", "Segment ends"),
        el("td", {}, el("button", { type: "button", class: "danger", onclick: () => {
          if (paused) return;
          data.segments.splice(i, 1);
          writeText(); renderTables();
        } }, "Delete"))));
    });
    showProblems();
  }

  // ---- wiring ----------------------------------------------------------------------------
  for (const b of root.querySelectorAll("[data-mode]")) {
    b.addEventListener("click", () => setMode(mode === b.dataset.mode ? null : b.dataset.mode));
  }
  finishButton.addEventListener("click", finishPolygon);
  cancelButton.addEventListener("click", () => setMode(null));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && mode) setMode(null); });
  document.getElementById("segment-add").addEventListener("click", () => {
    if (paused) return;
    const n = nextName("segment", data.segments);
    data.segments.push({ id: `segment_${n}`, start: 0, end: round(duration, 2) });
    writeText(); renderTables();
  });

  const showTime = () => {
    slider.value = String(video.currentTime);
    clock.textContent = video.currentTime.toFixed(2) + " s";
    draw();
  };
  slider.addEventListener("input", () => { video.currentTime = Number(slider.value); });
  for (const name of ["timeupdate", "seeked", "loadedmetadata"]) video.addEventListener(name, showTime);
  const play = document.getElementById("aoi-play");
  play.addEventListener("click", () => { if (video.paused) video.play(); else video.pause(); });
  video.addEventListener("play", () => { play.textContent = "Pause"; });
  video.addEventListener("pause", () => { play.textContent = "Play"; });
  new ResizeObserver(draw).observe(stage);

  textarea.addEventListener("input", readText); // hand edits in the text box
  root.hidden = false;
  readText();
});
