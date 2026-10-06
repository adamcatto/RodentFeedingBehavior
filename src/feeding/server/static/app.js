"use strict";
// Feeding-behaviour UI. Plain JS, no build step. All coordinates in the state
// are video-pixel coordinates; the canvas view transform maps them to screen.

const $ = (s) => document.querySelector(s);
const el = (tag, attrs = {}, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null) n.setAttribute(k, v);
  }
  for (const c of kids.flat()) if (c != null) n.append(c);
  return n;
};
const api = async (path, opts = {}) => {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!r.ok) throw new Error((await r.text()) || r.statusText);
  return r.json();
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

const POINTS = [
  ["center", "Centre", "C"], ["top", "Top", "T"], ["bottom", "Bottom", "B"], ["left", "Left", "L"], ["right", "Right", "R"],
];
const CHUNK = 600;

const S = {
  cfg: null, videos: [], sel: null, detail: null, tab: "bowl",
  frame: 0, nFrames: 1, fps: 30, w: 320, h: 240,
  img: null, renderToken: 0,
  draft: { shape: "ellipse", polygon: [] }, active: "center", dirty: false, rim: null,
  chunks: new Map(), timeline: null,
  playing: false, zoom: 1, zoomByTab: { bowl: 2, track: 1 }, drag: null,
  jobs: [], selJob: null, sheetOffset: 0,
};

// --------------------------------------------------------------------------- rim geometry (mirrors feeding/bowl.py)

// Ellipse from its four extremal points. Q = A^-1 = [[a, c], [c, d]] is the shape
// covariance; returned so the editor can transform the ellipse.
function ellipseFromExtremes(t, b, l, r) {
  const w = Math.abs(r[0] - l[0]) / 2, h = Math.abs(b[1] - t[1]) / 2;
  if (!(w > 0 && h > 0)) return null;
  let c = (((b[0] - t[0]) / 2) * h + ((r[1] - l[1]) / 2) * w) / 2;
  c = Math.max(-0.95 * w * h, Math.min(0.95 * w * h, c));
  const a = w * w, d = h * h;
  const mean = (a + d) / 2, diff = Math.sqrt(((a - d) / 2) ** 2 + c * c);
  const semi_major = Math.sqrt(mean + diff), semi_minor = Math.sqrt(mean - diff);
  return {
    type: "ellipse",
    cx: ((t[0] + b[0]) / 2 + (l[0] + r[0]) / 2) / 2,
    cy: ((t[1] + b[1]) / 2 + (l[1] + r[1]) / 2) / 2,
    semi_major, semi_minor, angle_rad: 0.5 * Math.atan2(2 * c, a - d),
    area_px2: Math.PI * semi_major * semi_minor, Q: [a, c, d],
  };
}

// Extremal points of the ellipse with centre m and covariance Q = [a, c, d].
function extremesFrom(m, Q) {
  const [a, c, d] = Q, sx = Math.sqrt(a), sy = Math.sqrt(d);
  return {
    top: round2([m[0] - c / sy, m[1] - sy]), bottom: round2([m[0] + c / sy, m[1] + sy]),
    left: round2([m[0] - sx, m[1] - c / sx]), right: round2([m[0] + sx, m[1] + c / sx]),
  };
}

// L Q L^T for 2x2 L and symmetric Q = [a, c, d].
function mulLQLt(L, Q) {
  const [a, c, d] = Q;
  const r0 = [L[0][0] * a + L[0][1] * c, L[0][0] * c + L[0][1] * d];
  const r1 = [L[1][0] * a + L[1][1] * c, L[1][0] * c + L[1][1] * d];
  return [r0[0] * L[0][0] + r0[1] * L[0][1], r0[0] * L[1][0] + r0[1] * L[1][1], r1[0] * L[1][0] + r1[1] * L[1][1]];
}

function polygonRim(pts) {
  if (!pts || pts.length < 3) return null;
  let a = 0, cx = 0, cy = 0;
  pts.forEach(([x0, y0], i) => {
    const [x1, y1] = pts[(i + 1) % pts.length], cr = x0 * y1 - x1 * y0;
    a += cr; cx += (x0 + x1) * cr; cy += (y0 + y1) * cr;
  });
  if (Math.abs(a) < 1e-9) return null;
  return { type: "polygon", points: pts, cx: cx / (3 * a), cy: cy / (3 * a), area_px2: Math.abs(a) / 2 };
}

function rimFromDraft(d) {
  if (d.shape === "polygon") return polygonRim(d.polygon);
  return d.top && d.bottom && d.left && d.right ? ellipseFromExtremes(d.top, d.bottom, d.left, d.right) : null;
}

function ellipseToPolygon(e, n) {
  const ca = Math.cos(e.angle_rad), sa = Math.sin(e.angle_rad);
  return Array.from({ length: n }, (_, i) => {
    const t = (2 * Math.PI * i) / n, u = e.semi_major * Math.cos(t), w = e.semi_minor * Math.sin(t);
    return [e.cx + ca * u - sa * w, e.cy + sa * u + ca * w];
  });
}

// Ellipse through a polygon's extremal vertices (topmost, bottommost, leftmost, rightmost).
function polygonExtremes(pts) {
  const by = (f) => pts.reduce((best, p) => (f(p) < f(best) ? p : best));
  return { top: by((p) => p[1]), bottom: by((p) => -p[1]), left: by((p) => p[0]), right: by((p) => -p[0]) };
}

// --------------------------------------------------------------------------- view transform

const stage = $("#stage");
const ctx = stage.getContext("2d");

function view() {
  const dpr = window.devicePixelRatio || 1;
  const cw = stage.width, k = (cw / S.w) * S.zoom;
  let fx = S.w / 2, fy = S.h / 2;
  const focus = S.draft.center || (S.rim && [S.rim.cx, S.rim.cy]);
  if (S.zoom > 1 && focus) [fx, fy] = focus;
  const vw = S.w / S.zoom, vh = S.h / S.zoom;
  const ox = Math.min(Math.max(fx - vw / 2, 0), S.w - vw), oy = Math.min(Math.max(fy - vh / 2, 0), S.h - vh);
  return { k, ox, oy, vw, vh, dpr };
}
const toScreen = (v, x, y) => [(x - v.ox) * v.k, (y - v.oy) * v.k];
function toImage(ev) {
  const v = view(), r = stage.getBoundingClientRect();
  const sx = ((ev.clientX - r.left) / r.width) * stage.width, sy = ((ev.clientY - r.top) / r.height) * stage.height;
  return [sx / v.k + v.ox, sy / v.k + v.oy];
}
function sizeStage() {
  const dpr = window.devicePixelRatio || 1;
  const cssW = stage.parentElement.clientWidth || 640;
  stage.width = Math.round(cssW * dpr);
  stage.height = Math.round((cssW * S.h / S.w) * dpr);
}

// --------------------------------------------------------------------------- data

async function loadImage(video, f) {
  const img = new Image();
  img.src = `/api/videos/${encodeURIComponent(video)}/frame/${f}`;
  await img.decode();
  return img;
}

async function chunk(video, ci) {
  const key = `${video}:${ci}`;
  if (!S.chunks.has(key)) {
    S.chunks.set(key, api(`/api/videos/${encodeURIComponent(video)}/poses?start=${ci * CHUNK}&count=${CHUNK}`).catch(() => null));
  }
  return S.chunks.get(key);
}

async function poseAt(video, f) {
  if (!S.detail?.has_predictions) return null;
  const c = await chunk(video, Math.floor(f / CHUNK));
  if (!c) return null;
  const i = f - c.start;
  if (i < 0 || i >= c.count) return null;
  const nodes = {};
  for (const [n, d] of Object.entries(c.nodes)) nodes[n] = [d.x[i], d.y[i], d.s[i]];
  return { nodes, valid: !!c.valid[i], in_bowl: !!c.in_bowl[i] };
}

async function trailAt(video, f, len) {
  const out = [];
  const node = S.cfg.bout_node;
  for (let g = Math.max(0, f - len); g <= f; g++) {
    const c = await chunk(video, Math.floor(g / CHUNK));
    if (!c) continue;
    const i = g - c.start;
    out.push([c.nodes[node].x[i], c.nodes[node].y[i]]);
  }
  return out;
}

// --------------------------------------------------------------------------- drawing

function drawRim(c, v, rim, color, width = 2) {
  if (!rim) return;
  c.save();
  c.strokeStyle = color; c.lineWidth = width * v.dpr;
  c.beginPath();
  if (rim.type === "polygon") {
    rim.points.forEach((p, i) => { const [x, y] = toScreen(v, p[0], p[1]); i ? c.lineTo(x, y) : c.moveTo(x, y); });
    c.closePath();
  } else {
    const [x, y] = toScreen(v, rim.cx, rim.cy);
    c.ellipse(x, y, rim.semi_major * v.k, rim.semi_minor * v.k, rim.angle_rad, 0, 2 * Math.PI);
  }
  c.stroke();
  c.restore();
}

function drawCross(c, v, p, color, size = 6) {
  const [x, y] = toScreen(v, p[0], p[1]), s = size * v.dpr;
  c.save(); c.strokeStyle = color; c.lineWidth = 1.5 * v.dpr;
  c.beginPath(); c.moveTo(x - s, y); c.lineTo(x + s, y); c.moveTo(x, y - s); c.lineTo(x, y + s); c.stroke();
  c.restore();
}

function drawSkeleton(c, v, pose, opts) {
  if (!pose) return;
  const accent = css("--accent"), bowl = css("--bowl"), minS = S.cfg.min_score;
  c.save();
  c.lineCap = "round";
  if (opts.skeleton) {
    for (const [a, b] of S.cfg.edges) {
      const pa = pose.nodes[a], pb = pose.nodes[b];
      if (pa?.[0] == null || pb?.[0] == null) continue;
      const [x1, y1] = toScreen(v, pa[0], pa[1]), [x2, y2] = toScreen(v, pb[0], pb[1]);
      c.strokeStyle = "rgba(0,0,0,.55)"; c.lineWidth = 4 * v.dpr;
      c.beginPath(); c.moveTo(x1, y1); c.lineTo(x2, y2); c.stroke();
      c.strokeStyle = "rgba(255,255,255,.85)"; c.lineWidth = 1.6 * v.dpr;
      c.beginPath(); c.moveTo(x1, y1); c.lineTo(x2, y2); c.stroke();
    }
  }
  for (const [n, p] of Object.entries(pose.nodes)) {
    if (p[0] == null) continue;
    const [x, y] = toScreen(v, p[0], p[1]);
    const isBoutNode = n === S.cfg.bout_node, r = (isBoutNode ? 4.5 : 3.5) * v.dpr;
    const low = p[2] == null || p[2] < minS;
    c.beginPath(); c.arc(x, y, r, 0, 2 * Math.PI);
    c.lineWidth = 2 * v.dpr; c.strokeStyle = "#fff";
    c.fillStyle = isBoutNode && pose.in_bowl ? bowl : accent;
    if (low) { c.stroke(); } else { c.fill(); c.stroke(); }
    if (isBoutNode && pose.in_bowl) {
      c.beginPath(); c.arc(x, y, r + 4 * v.dpr, 0, 2 * Math.PI);
      c.strokeStyle = bowl; c.lineWidth = 2 * v.dpr; c.stroke();
    }
  }
  c.restore();
}

function drawTrail(c, v, pts) {
  const color = css("--accent");
  c.save(); c.strokeStyle = color; c.lineWidth = 1.5 * v.dpr;
  for (let i = 1; i < pts.length; i++) {
    const [a, b] = [pts[i - 1], pts[i]];
    if (a[0] == null || b[0] == null) continue;
    c.globalAlpha = i / pts.length;
    const [x1, y1] = toScreen(v, a[0], a[1]), [x2, y2] = toScreen(v, b[0], b[1]);
    c.beginPath(); c.moveTo(x1, y1); c.lineTo(x2, y2); c.stroke();
  }
  c.restore();
}

function drawBowlEditor(c, v) {
  const bowl = css("--bowl"), poly = S.draft.shape === "polygon";
  drawRim(c, v, S.rim, bowl, 2);
  if (poly && !S.rim && S.draft.polygon.length > 1) drawRim(c, v, { type: "polygon", points: S.draft.polygon }, bowl, 1);
  for (const h of handles()) {
    if (h.key === "center") drawCross(c, v, h.p, bowl, 8);
    const [x, y] = toScreen(v, h.p[0], h.p[1]), r = (poly && h.key !== "center" ? 3.5 : 5) * v.dpr;
    c.save();
    c.beginPath(); c.arc(x, y, r, 0, 2 * Math.PI);
    c.fillStyle = h.key === S.active ? bowl : "rgba(0,0,0,.6)"; c.fill();
    c.lineWidth = 1.5 * v.dpr; c.strokeStyle = "#fff"; c.stroke();
    if (!poly || h.key === "center" || h.key === S.active) {
      c.fillStyle = "#fff"; c.font = `${11 * v.dpr}px ${css("--font")}`;
      c.fillText(h.label, x + 8 * v.dpr, y - 6 * v.dpr);
    }
    c.restore();
  }
}

async function render() {
  if (!S.sel) return;
  const token = ++S.renderToken, video = S.sel, f = S.frame;
  let img, pose = null, trail = [];
  try {
    img = await loadImage(video, f);
    if (S.tab === "track") {
      pose = await poseAt(video, f);
      if ($("#opt-trail").checked && pose) trail = await trailAt(video, f, Math.round(S.fps));
    }
  } catch (e) {
    if (token === S.renderToken) msg(`Could not load frame ${f}: ${e.message}`);
    return;
  }
  if (token !== S.renderToken) return;
  S.img = img;
  const v = view();
  ctx.clearRect(0, 0, stage.width, stage.height);
  ctx.imageSmoothingEnabled = S.zoom < 2;
  ctx.drawImage(img, v.ox, v.oy, v.vw, v.vh, 0, 0, stage.width, stage.height);
  if (S.tab === "bowl") drawBowlEditor(ctx, v);
  if (S.tab === "track") {
    if ($("#opt-bowl").checked) {
      drawRim(ctx, v, S.detail?.bowl?.rim, css("--bowl"), 1.5);
      if (S.detail?.bowl) drawCross(ctx, v, S.detail.bowl.center, css("--bowl"), 5);
    }
    drawTrail(ctx, v, trail);
    drawSkeleton(ctx, v, pose, { skeleton: $("#opt-skel").checked });
    updateTrackState(pose);
    drawTimeline();
  }
  msg(null);
  syncFrameControls();
}

function msg(text) {
  const m = $("#stage-msg");
  m.hidden = !text; m.textContent = text || "";
}

// --------------------------------------------------------------------------- timeline

const tl = $("#timeline");
function drawTimeline() {
  const t = S.timeline, c = tl.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  tl.width = Math.round(tl.clientWidth * dpr); tl.height = Math.round(44 * dpr);
  c.clearRect(0, 0, tl.width, tl.height);
  if (!t) return;
  const W = tl.width, H = tl.height, n = t.valid.length, bw = W / n;
  const top = 10 * dpr, bh = H - top;
  for (let i = 0; i < n; i++) {
    c.fillStyle = (t.valid[i] ?? 0) >= 0.5 ? css("--tl-valid") : css("--tl-invalid");
    c.fillRect(i * bw, top, Math.ceil(bw), bh);
    const ib = t.in_bowl[i] ?? 0;
    if (ib > 0) {
      const hh = Math.max(2 * dpr, bh * Math.min(1, ib * 2));
      c.fillStyle = css("--tl-bowl");
      c.fillRect(i * bw, H - hh, Math.ceil(bw), hh);
    }
  }
  c.fillStyle = css("--bowl");
  for (const b of t.bouts) {
    const x0 = (b.window_start / t.n_frames) * W, x1 = (b.window_end / t.n_frames) * W;
    const c0 = (b.contact_start / t.n_frames) * W, c1 = (b.contact_end / t.n_frames) * W;
    c.globalAlpha = 0.45; c.fillRect(x0, 4 * dpr, Math.max(1, x1 - x0), 2 * dpr);
    c.globalAlpha = 1; c.fillRect(c0, 2 * dpr, Math.max(2 * dpr, c1 - c0), 6 * dpr);
  }
  const px = (S.frame / t.n_frames) * W;
  c.fillStyle = css("--text"); c.fillRect(px - dpr, 0, 2 * dpr, H);
}
tl.addEventListener("click", (ev) => {
  if (!S.timeline) return;
  const r = tl.getBoundingClientRect();
  seek(Math.round(((ev.clientX - r.left) / r.width) * S.timeline.n_frames));
});

function updateTrackState(pose) {
  const chip = $("#track-state");
  if (!S.detail?.has_predictions) { chip.textContent = "No SLEAP predictions yet"; chip.className = "state-chip"; return; }
  if (!pose) { chip.textContent = "—"; chip.className = "state-chip"; return; }
  if (!S.detail.bowl) { chip.textContent = pose.valid ? "Tracked (no bowl annotated)" : "Low confidence"; chip.className = "state-chip"; return; }
  chip.textContent = pose.in_bowl ? "Snout in bowl" : pose.valid ? "Snout outside bowl" : "Low confidence (excluded)";
  chip.className = "state-chip" + (pose.in_bowl ? " in" : "");
}

function renderTrackSummary() {
  const dl = $("#track-summary"), nav = $("#bout-nav");
  dl.replaceChildren(); nav.replaceChildren();
  const t = S.timeline;
  if (!t) return;
  const pct = (a) => `${((100 * a) / t.n_frames).toFixed(1)}%`;
  const rows = [["frames", t.n_frames], ["tracked", `${t.summary.valid_frames} (${pct(t.summary.valid_frames)})`]];
  if (S.detail?.bowl) {
    rows.push(["in bowl", `${t.summary.in_bowl_frames} (${(t.summary.in_bowl_frames / S.fps).toFixed(1)} s)`]);
    rows.push(["bouts", t.summary.n_bouts]);
  }
  for (const [k, v] of rows) dl.append(el("dt", {}, k), el("dd", {}, String(v)));
  if (t.bouts.length) {
    const jump = (dir) => {
      const starts = t.bouts.map((b) => b.contact_start);
      const target = dir > 0 ? starts.find((s) => s > S.frame + 1) : [...starts].reverse().find((s) => s < S.frame - 1);
      if (target != null) seek(target);
    };
    nav.append(el("button", { class: "ghost small", onclick: () => jump(-1) }, "◀ Previous bout"),
               el("button", { class: "ghost small", onclick: () => jump(1) }, "Next bout ▶"));
  }
}

// --------------------------------------------------------------------------- frame controls

function syncFrameControls() {
  $("#frame-slider").value = S.frame;
  $("#frame-input").value = S.frame;
  const s = S.frame / S.fps;
  $("#frame-time").textContent = `${Math.floor(s / 60)}:${(s % 60).toFixed(1).padStart(4, "0")} / ${S.nFrames} frames`;
}
function seek(f) {
  S.frame = Math.max(0, Math.min(S.nFrames - 1, Math.round(f)));
  syncFrameControls();
  updateHash();
  render();
}
const updateHash = () => history.replaceState(null, "", `#${encodeURIComponent(S.sel || "")}/${S.tab}/${S.frame}`);
const step = () => Math.max(1, parseInt($("#step").value, 10) || 1);
$("#frame-slider").addEventListener("input", (e) => seek(+e.target.value));
$("#frame-input").addEventListener("change", (e) => seek(+e.target.value));
$("#btn-prev").addEventListener("click", () => seek(S.frame - step()));
$("#btn-next").addEventListener("click", () => seek(S.frame + step()));
$("#btn-play").addEventListener("click", () => togglePlay());
$("#zoom").addEventListener("change", (e) => { S.zoom = S.zoomByTab[S.tab] = +e.target.value; render(); });
for (const id of ["#opt-skel", "#opt-trail", "#opt-bowl"]) $(id).addEventListener("change", render);

async function togglePlay(force) {
  S.playing = force ?? !S.playing;
  $("#btn-play").textContent = S.playing ? "❚❚" : "▶";
  while (S.playing) {
    const t0 = performance.now();
    if (S.frame + step() >= S.nFrames) { togglePlay(false); break; }
    S.frame += step();
    await render();
    await sleep(Math.max(0, +$("#interval").value - (performance.now() - t0)));
  }
}

// --------------------------------------------------------------------------- bowl editor
// Draft: { shape: "ellipse" | "polygon", center, top, bottom, left, right, polygon: [[x, y], ...] }.
// S.active is a key ("center", "top", ...) or a polygon vertex index.

const round2 = (p) => [Math.round(p[0] * 100) / 100, Math.round(p[1] * 100) / 100];
const getPt = (key) => (typeof key === "number" ? S.draft.polygon[key] : S.draft[key]);
const setPt = (key, p) => { if (typeof key === "number") S.draft.polygon[key] = round2(p); else S.draft[key] = round2(p); };

function setDraft(bowl) {
  S.draft = { shape: bowl?.shape || "ellipse", polygon: bowl?.polygon ? bowl.polygon.map((p) => [...p]) : [] };
  for (const [k] of POINTS) if (bowl?.[k]) S.draft[k] = [...bowl[k]];
  S.active = S.draft.shape === "polygon" ? (S.draft.center ? S.draft.polygon.length - 1 : "center")
    : (POINTS.find(([k]) => !S.draft[k]) || POINTS[0])[0];
  S.dirty = false;
  updateRim();
}

function updateRim() {
  S.rim = rimFromDraft(S.draft);
  renderBowlPanel();
}

function handles() {
  const d = S.draft, out = [];
  if (d.center) out.push({ key: "center", p: d.center, label: "C" });
  if (d.shape === "ellipse") {
    for (const [k, , short] of POINTS.slice(1)) if (d[k]) out.push({ key: k, p: d[k], label: short });
  } else {
    d.polygon.forEach((p, i) => out.push({ key: i, p, label: String(i + 1) }));
  }
  return out;
}

// Apply x -> pivot + L (x - pivot) + t to the whole bowl (centre and rim). The pivot
// is the clicked centre (else the rim centre). For an ellipse the rim's centre and
// covariance are transformed and the four extremal points recomputed from them.
function transformBowl(L, t = [0, 0]) {
  const d = S.draft, rim = S.rim;
  const pv = d.center || (rim && [rim.cx, rim.cy]);
  if (!pv) return;
  const map = (p) => [pv[0] + L[0][0] * (p[0] - pv[0]) + L[0][1] * (p[1] - pv[1]) + t[0],
                      pv[1] + L[1][0] * (p[0] - pv[0]) + L[1][1] * (p[1] - pv[1]) + t[1]];
  if (d.shape === "polygon") d.polygon = d.polygon.map((p) => round2(map(p)));
  else if (rim) Object.assign(d, extremesFrom(map([rim.cx, rim.cy]), mulLQLt(L, rim.Q)));
  if (d.center) d.center = round2(map(d.center));
  S.dirty = true; updateRim(); render();
}
const rot = (deg) => { const a = (deg * Math.PI) / 180; return [[Math.cos(a), -Math.sin(a)], [Math.sin(a), Math.cos(a)]]; };
const ID = [[1, 0], [0, 1]];
const TOOLS = {
  "move-left": () => transformBowl(ID, [-1, 0]), "move-right": () => transformBowl(ID, [1, 0]),
  "move-up": () => transformBowl(ID, [0, -1]), "move-down": () => transformBowl(ID, [0, 1]),
  "rot-ccw": () => transformBowl(rot(-3)), "rot-cw": () => transformBowl(rot(3)),
  "shrink": () => transformBowl([[0.97, 0], [0, 0.97]]), "grow": () => transformBowl([[1.03, 0], [0, 1.03]]),
  "narrower": () => transformBowl([[0.97, 0], [0, 1]]), "wider": () => transformBowl([[1.03, 0], [0, 1]]),
  "shorter": () => transformBowl([[1, 0], [0, 0.97]]), "taller": () => transformBowl([[1, 0], [0, 1.03]]),
};
document.querySelectorAll("[data-tool]").forEach((b) => b.addEventListener("click", () => TOOLS[b.dataset.tool]()));

function setShape(shape) {
  const d = S.draft;
  if (shape === d.shape) return;
  if (shape === "polygon" && S.rim) d.polygon = ellipseToPolygon(S.rim, 24).map(round2);
  if (shape === "ellipse" && S.rim) Object.assign(d, polygonExtremes(d.polygon));
  d.shape = shape;
  S.active = shape === "polygon" ? (d.polygon.length ? d.polygon.length - 1 : "center") : "center";
  S.dirty = true; updateRim(); render();
}
document.querySelectorAll("#bowl-shape button").forEach((b) => b.addEventListener("click", () => setShape(b.dataset.shape)));

function renderBowlPanel() {
  const d = S.draft, poly = d.shape === "polygon";
  document.querySelectorAll("#bowl-shape button").forEach((b) => b.classList.toggle("on", b.dataset.shape === d.shape));
  $("#bowl-help").textContent = poly
    ? "Click to add a vertex after the selected one; drag to move it; Alt-click or Delete removes it. Select Centre and click to place the centre."
    : "Click to place the selected point (it then advances). Drag a point to adjust it.";
  const row = (key, label, value) => el("li", {
    class: S.active === key ? "active" : "", onclick: () => { S.active = key; renderBowlPanel(); render(); },
  }, el("span", {}, label), el("span", { class: "coord" }, value));
  const fmt = (p) => (p ? p.map((x) => x.toFixed(1)).join(", ") : "—");
  $("#bowl-points").replaceChildren(...(poly
    ? [row("center", "1. Centre", fmt(d.center)),
       row(typeof S.active === "number" ? S.active : d.polygon.length - 1, "Vertices",
           `${d.polygon.length}${typeof S.active === "number" && S.active >= 0 ? ` · #${S.active + 1} selected` : ""}`)]
    : POINTS.map(([k, label], i) => row(k, `${i + 1}. ${label}`, fmt(d[k])))));
  const r = S.rim, stats = $("#bowl-stats");
  stats.textContent = !r ? (poly ? "Add at least 3 vertices." : "Place top, bottom, left and right to see the rim.")
    : r.type === "polygon" ? `${r.points.length} vertices\narea ${r.area_px2.toFixed(0)} px²`
    : `semi-axes ${r.semi_major.toFixed(1)} × ${r.semi_minor.toFixed(1)} px\ntilt ${((r.angle_rad * 180) / Math.PI).toFixed(0)}°  area ${r.area_px2.toFixed(0)} px²`;
  $("#bowl-tools").classList.toggle("disabled", !(S.rim || d.center));
  $("#bowl-save").disabled = !(d.center && r && S.dirty);
  $("#bowl-delete").disabled = !S.detail?.bowl;
  $("#bowl-reset").disabled = !S.dirty;
}

function hitHandle(ev) {
  const v = view(), r = stage.getBoundingClientRect(), scale = stage.width / r.width;
  const sx = (ev.clientX - r.left) * scale, sy = (ev.clientY - r.top) * scale;
  let best = null, bestD = 10 * v.dpr;
  for (const h of handles()) {
    const [x, y] = toScreen(v, h.p[0], h.p[1]), d = Math.hypot(x - sx, y - sy);
    if (d < bestD) { best = h.key; bestD = d; }
  }
  return best;
}

function deleteVertex(i) {
  S.draft.polygon.splice(i, 1);
  S.active = S.draft.polygon.length ? Math.min(i, S.draft.polygon.length - 1) : "center";
  S.dirty = true; updateRim(); render();
}

stage.addEventListener("mousedown", (ev) => {
  if (S.tab !== "bowl" || !S.sel) return;
  const d = S.draft, p = toImage(ev);
  if (ev.shiftKey && (S.rim || d.center)) { S.drag = { mode: "move", last: p }; return; }
  const h = hitHandle(ev);
  if (h !== null) {
    if (ev.altKey && typeof h === "number") { deleteVertex(h); return; }
    S.active = h; S.drag = { mode: "point" }; renderBowlPanel(); render(); return;
  }
  if (d.shape === "polygon" && S.active !== "center") {
    const at = typeof S.active === "number" ? S.active + 1 : d.polygon.length;
    d.polygon.splice(at, 0, round2(p));
    S.active = at;
  } else if (d.shape === "polygon") {
    d.center = round2(p);
    S.active = d.polygon.length - 1;  // next clicks append vertices
  } else {
    setPt(S.active, p);
    const order = POINTS.map(([k]) => k), i = order.indexOf(S.active);
    S.active = order.slice(i + 1).find((k) => !d[k]) ?? order.find((k) => !d[k]) ?? S.active;
  }
  S.dirty = true; updateRim(); render();
});
window.addEventListener("mousemove", (ev) => {
  if (!S.drag) return;
  const p = toImage(ev);
  if (S.drag.mode === "move") {
    const t = [p[0] - S.drag.last[0], p[1] - S.drag.last[1]];
    S.drag.last = p;
    transformBowl(ID, t);
    return;
  }
  setPt(S.active, p);
  S.dirty = true; updateRim(); render();
});
window.addEventListener("mouseup", () => { S.drag = null; });

$("#bowl-save").addEventListener("click", async () => {
  const d = S.draft;
  const body = { shape: d.shape, center: d.center, frame_idx: S.frame };
  if (d.shape === "polygon") body.polygon = d.polygon;
  else for (const [k] of POINTS.slice(1)) body[k] = d[k];
  try {
    const saved = await api(`/api/videos/${encodeURIComponent(S.sel)}/bowl`, { method: "PUT", body: JSON.stringify(body) });
    S.detail.bowl = saved;
    setDraft(saved);
    S.chunks.clear();
    $("#bowl-msg").textContent = `Saved ${new Date().toLocaleTimeString()}`;
    await refreshVideos();
    await loadTimeline();
    render();
  } catch (e) { $("#bowl-msg").textContent = `Save failed: ${e.message}`; }
});
$("#bowl-reset").addEventListener("click", () => { setDraft(S.detail?.bowl); render(); });
$("#bowl-delete").addEventListener("click", async () => {
  if (!confirm(`Delete the bowl annotation for ${S.sel}?`)) return;
  await api(`/api/videos/${encodeURIComponent(S.sel)}/bowl`, { method: "DELETE" });
  S.detail.bowl = null; setDraft(null); S.chunks.clear();
  await refreshVideos(); await loadTimeline(); render();
});
$("#bowl-copy").addEventListener("change", async (e) => {
  if (!e.target.value) return;
  const other = await api(`/api/videos/${encodeURIComponent(e.target.value)}`);
  if (other.bowl) {
    setDraft(other.bowl); S.dirty = true; renderBowlPanel(); render();
    $("#bowl-msg").textContent = `Copied from ${e.target.value}. Shift-drag or use the tools to fit it to this video, then save.`;
  }
  e.target.value = "";
});

function fillCopySelect() {
  const cur = S.videos.find((v) => v.video === S.sel);
  const opts = S.videos.filter((v) => v.has_bowl && v.video !== S.sel);
  const score = (v) => (cur && v.animal === cur.animal && v.context === cur.context ? 0 : cur && v.context === cur.context ? 1 : 2);
  opts.sort((a, b) => score(a) - score(b) || a.video.localeCompare(b.video, undefined, { numeric: true }));
  $("#bowl-copy").replaceChildren(el("option", { value: "" }, opts.length ? "—" : "no other annotations yet"),
    ...opts.map((v) => el("option", { value: v.video }, `${v.video}${score(v) === 0 ? "  (same animal)" : ""}`)));
}

window.addEventListener("keydown", (ev) => {
  if (["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement?.tagName) || document.querySelector("dialog[open]")) return;
  if (ev.key === "?") { showHelp(); return; }
  if (!S.sel || $("#detail").hidden) return;
  if (ev.key === " ") { ev.preventDefault(); togglePlay(); return; }
  const arrows = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
  if (S.tab === "bowl") {
    const d = S.draft;
    if (d.shape === "ellipse" && /^[1-5]$/.test(ev.key)) { S.active = POINTS[+ev.key - 1][0]; renderBowlPanel(); render(); return; }
    const tool = { "[": "rot-ccw", "]": "rot-cw", "-": "shrink", "=": "grow", "+": "grow" }[ev.key];
    if (tool) { TOOLS[tool](); return; }
    if ((ev.key === "Delete" || ev.key === "Backspace") && d.shape === "polygon" && typeof S.active === "number" && S.active >= 0) {
      ev.preventDefault(); deleteVertex(S.active); return;
    }
    if (ev.key in arrows) {
      ev.preventDefault();
      const [dx, dy] = arrows[ev.key];
      if (ev.altKey) { const s = ev.shiftKey ? 5 : 1; transformBowl(ID, [dx * s, dy * s]); return; }
      const p = getPt(S.active);
      if (p) {
        const s = ev.shiftKey ? 2 : 0.5;
        setPt(S.active, [p[0] + dx * s, p[1] + dy * s]);
        S.dirty = true; updateRim(); render();
        return;
      }
    }
  }
  if (ev.key === "ArrowLeft" || ev.key === "ArrowRight") { ev.preventDefault(); seek(S.frame + arrows[ev.key][0] * step()); }
});

// --------------------------------------------------------------------------- overview sheet

async function renderSheet() {
  const box = $("#sheet");
  box.replaceChildren();
  const n = 12, spacing = S.nFrames / n;
  const frames = Array.from({ length: n }, (_, i) => Math.min(S.nFrames - 1, Math.floor(i * spacing + (S.sheetOffset % spacing))));
  const video = S.sel;
  for (const f of frames) {
    const cv = el("canvas", { width: S.w * 2, height: S.h * 2 });
    const fig = el("figure", { onclick: () => { S.frame = f; setTab("track"); } }, cv, el("figcaption", {}, `frame ${f}`));
    box.append(fig);
    (async () => {
      const [img, pose] = await Promise.all([loadImage(video, f), poseAt(video, f)]);
      if (S.sel !== video) return;
      const c = cv.getContext("2d"), v = { k: 2, ox: 0, oy: 0, dpr: 1 };
      c.drawImage(img, 0, 0, cv.width, cv.height);
      drawRim(c, v, S.detail?.bowl?.rim, css("--bowl"), 1.5);
      drawSkeleton(c, v, pose, { skeleton: true });
      fig.querySelector("figcaption").textContent = `frame ${f}` + (pose ? (pose.in_bowl ? " · in bowl" : pose.valid ? "" : " · low confidence") : "");
    })().catch(() => {});
  }
}
$("#sheet-shift").addEventListener("click", () => { S.sheetOffset += S.nFrames / 12 / 3; renderSheet(); });

// --------------------------------------------------------------------------- tabs & selection

function setTab(tab) {
  S.tab = tab;
  togglePlay(false);
  document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  $("#panel-bowl").hidden = tab !== "bowl";
  $("#panel-track").hidden = tab !== "track";
  $("#panel-sheet").hidden = tab !== "sheet";
  $("#timeline-wrap").hidden = tab !== "track";
  $("#sheet").hidden = tab !== "sheet";
  document.querySelector(".stage-col").hidden = tab === "sheet";
  S.zoom = S.zoomByTab[tab] ?? 1;
  $("#zoom").value = String(S.zoom);
  updateHash();
  if (tab === "sheet") { renderSheet(); return; }
  sizeStage();
  if (tab === "track") { renderTrackSummary(); requestAnimationFrame(drawTimeline); }
  render();
}
document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => setTab(b.dataset.tab)));

async function loadTimeline() {
  S.timeline = null;
  if (S.detail?.has_predictions) {
    try { S.timeline = await api(`/api/videos/${encodeURIComponent(S.sel)}/timeline`); } catch { S.timeline = null; }
  }
  renderTrackSummary();
  drawTimeline();
}

async function selectVideo(name, tab, frame) {
  togglePlay(false);
  S.sel = name; S.chunks.clear(); S.timeline = null; S.sheetOffset = 0;
  document.querySelectorAll("#video-list li").forEach((li) => li.classList.toggle("active", li.dataset.video === name));
  $("#empty").hidden = true; $("#results").hidden = true; $("#settings").hidden = true; $("#help").hidden = true; $("#detail").hidden = false;
  S.detail = await api(`/api/videos/${encodeURIComponent(name)}`);
  const p = S.detail.props;
  Object.assign(S, { w: p.width, h: p.height, fps: p.fps || 30, nFrames: p.n_frames });
  S.frame = Number.isFinite(frame) ? Math.min(S.nFrames - 1, frame)
    : S.detail.bowl?.frame_idx ?? Math.min(S.nFrames - 1, Math.round(S.fps * 60));
  $("#frame-slider").max = S.nFrames - 1;
  $("#frame-input").max = S.nFrames - 1;
  $("#v-name").textContent = name;
  const d = S.detail;
  $("#v-meta").textContent = `${d.condition} · context ${d.context}${d.chamber ? ` (${d.chamber})` : ""} · animal ${d.animal}` +
    `${S.videos.find((v) => v.video === name)?.group ? " · " + S.videos.find((v) => v.video === name).group : ""}` +
    ` · ${p.width}×${p.height} @ ${p.fps.toFixed(2)} fps`;
  setDraft(d.bowl);
  fillCopySelect();
  $("#bowl-msg").textContent = d.bowl?.annotated_at ? `Saved ${new Date(d.bowl.annotated_at).toLocaleString()} on frame ${d.bowl.frame_idx}` : "";
  updateHeadActions();
  sizeStage();
  await loadTimeline();
  setTab(tab || (d.bowl ? "track" : "bowl"));
}

function updateHeadActions() {
  const v = S.videos.find((x) => x.video === S.sel), btn = $("#btn-infer-one"), st = $("#v-pred-status");
  if (!v) return;
  const job = v.job;
  if (job) {
    const p = job.progress || {};
    st.textContent = job.status === "queued" ? "SLEAP queued" :
      `SLEAP ${p.n_total ? Math.round((100 * p.n_processed) / p.n_total) + "%" : "starting"}${p.eta ? `, ~${Math.round(p.eta / 60)} min left` : ""}`;
    btn.textContent = "Cancel"; btn.className = "ghost danger";
    btn.onclick = () => api(`/api/jobs/${job.id}/cancel`, { method: "POST" }).then(pollJobs);
  } else {
    const src = S.detail?.provenance?.source;
    const models = S.detail?.models || [];
    const modelName = models.map((m) => m.split("/").pop()).join(" + ") || "no model configured";
    const srcLabel = !src || src === "sleap-track" ? "SLEAP " + (S.detail?.provenance?.sleap_version || "") : src === "demo" ? "synthetic (demo)" : src;
    st.textContent = (v.has_predictions ? `Predictions: ${srcLabel}` : "No predictions") +
      ` · model ${modelName}` + (S.detail?.predictions_model_differs ? " · predictions were made with a different model" : "") +
      (S.detail?.predictions_params_differ?.length ? ` · made with different SLEAP settings (${S.detail.predictions_params_differ.join(", ")})` : "");
    st.classList.toggle("stale", !!S.detail?.predictions_model_differs || !!S.detail?.predictions_params_differ?.length);
    st.title = models.join("\n");
    btn.textContent = v.has_predictions ? "Re-run SLEAP" : "Run SLEAP";
    btn.className = v.has_predictions ? "ghost" : "";
    btn.onclick = async () => {
      if (v.has_predictions && !confirm(`Overwrite the existing predictions for ${v.video}?`)) return;
      await api("/api/jobs/infer", { method: "POST", body: JSON.stringify({ videos: [v.video], force: v.has_predictions }) });
      pollJobs();
    };
  }
}

// --------------------------------------------------------------------------- sidebar

async function refreshVideos() {
  if (!S.project?.open) { S.videos = []; renderList(); return; }
  S.videos = await api("/api/videos");
  const conds = [...new Set(S.videos.map((v) => v.condition))];
  const sel = $("#filter-condition"), keepC = sel.value;
  sel.replaceChildren(el("option", { value: "" }, "All conditions"), ...conds.map((c) => el("option", { value: c }, c)));
  sel.value = conds.includes(keepC) ? keepC : "";
  const ctxs = [...new Map(S.videos.filter((v) => v.context !== "NA").map((v) => [v.context, v.chamber])).entries()].sort();
  const csel = $("#filter-context"), keepX = csel.value;
  csel.replaceChildren(el("option", { value: "" }, "All contexts"), ...ctxs.map(([c, ch]) => el("option", { value: c }, ch ? `${c} · ${ch}` : c)));
  csel.value = ctxs.some(([c]) => c === keepX) ? keepX : "";
  csel.hidden = !ctxs.length; sel.hidden = conds.every((c) => c === "NA");
  renderList();
  if (S.sel) updateHeadActions();
}

function renderList() {
  const q = $("#filter-text").value.trim().toLowerCase(), ctxF = $("#filter-context").value;
  const condF = $("#filter-condition").value, stF = $("#filter-status").value;
  const vs = S.videos.filter((v) =>
    (!q || v.video.toLowerCase().includes(q)) && (!ctxF || v.context === ctxF) && (!condF || v.condition === condF) &&
    (!stF || (stF === "needs-pred" && !v.has_predictions) || (stF === "needs-bowl" && !v.has_bowl) ||
      (stF === "ready" && v.has_predictions && v.has_bowl)));
  const ready = S.videos.filter((v) => v.has_predictions && v.has_bowl).length;
  $("#list-summary").textContent = `${vs.length} shown · ${S.videos.filter((v) => v.has_predictions).length} with predictions · ` +
    `${S.videos.filter((v) => v.has_bowl).length} with bowl · ${ready} ready`;
  $("#video-list").replaceChildren(...vs.map((v) => {
    const job = v.job, p = job?.progress || {};
    const pct = job && p.n_total ? (100 * p.n_processed) / p.n_total : 0;
    return el("li", { "data-video": v.video, class: v.video === S.sel ? "active" : "", onclick: () => selectVideo(v.video) },
      el("span", { class: "vname" }, v.video),
      el("span", { class: "badges" },
        el("span", { class: "badge " + (job ? "run" : v.has_predictions ? "on" : ""), title: job ? `SLEAP ${job.status}` : v.has_predictions ? "SLEAP predictions" : "no predictions" }, "P"),
        el("span", { class: "badge " + (v.has_bowl ? "on" : ""), title: v.has_bowl ? "bowl annotated" : "bowl not annotated" }, "B")),
      el("span", { class: "vmeta" }, `${v.condition} · ${v.context} · #${v.animal}${v.group ? " · " + v.group : ""}`),
      job ? el("span", { class: "minibar" }, el("i", { style: `width:${pct}%` })) : null);
  }));
}
for (const id of ["#filter-text", "#filter-context", "#filter-condition", "#filter-status"]) $(id).addEventListener("input", renderList);

// --------------------------------------------------------------------------- jobs

let lastActive = 0;
async function pollJobs() {
  try { S.jobs = await api("/api/jobs"); } catch { return; }
  const active = S.jobs.filter((j) => j.status === "running" || j.status === "queued").length;
  $("#jobs-count").textContent = active || "";
  if (S.project?.open && (active || active !== lastActive)) {
    const wasPred = S.videos.find((v) => v.video === S.sel)?.has_predictions;
    await refreshVideos();
    const nowPred = S.videos.find((v) => v.video === S.sel)?.has_predictions;
    if (S.sel && nowPred && !wasPred) await selectVideo(S.sel, S.tab);
  }
  lastActive = active;
  renderJobs();
  resultsJobsChanged(S.jobs);
}
setInterval(pollJobs, 2000);

function renderJobs() {
  if ($("#jobs-drawer").hidden) return;
  $("#jobs-list").replaceChildren(...(S.jobs.length ? S.jobs.map((j) => {
    const p = j.progress || {}, pct = p.n_total ? (100 * p.n_processed) / p.n_total : j.status === "done" ? 100 : 0;
    return el("li", { class: S.selJob === j.id ? "sel" : "", onclick: () => { S.selJob = j.id; showLog(); renderJobs(); } },
      el("div", { class: "row" }, el("span", {}, j.label), el("span", { class: "st-" + j.status }, j.status)),
      el("div", { class: "muted" }, j.project_name ? `project ${j.project_name}` : ""),
      j.kind === "infer" && j.status === "running" ? el("div", { class: "progress" }, el("i", { style: `width:${pct}%` })) : null,
      j.error ? el("div", { class: "small", style: "color:var(--bad)" }, j.error) : null,
      j.status === "running" || j.status === "queued"
        ? el("button", { class: "ghost small danger", onclick: (e) => { e.stopPropagation(); api(`/api/jobs/${j.id}/cancel`, { method: "POST" }).then(pollJobs); } }, "Cancel")
        : null);
  }) : [el("li", { class: "muted" }, "No jobs yet.")]));
  if (S.selJob) showLog();
}
async function showLog() {
  const pre = $("#job-log");
  try {
    const j = await api(`/api/jobs/${S.selJob}`);
    pre.hidden = false;
    const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 4;
    pre.textContent = j.log.join("\n") || "(no output yet)";
    if (atBottom) pre.scrollTop = pre.scrollHeight;
  } catch { pre.hidden = true; }
}
$("#btn-jobs").addEventListener("click", () => { $("#jobs-drawer").hidden = !$("#jobs-drawer").hidden; renderJobs(); });
$("#jobs-close").addEventListener("click", () => { $("#jobs-drawer").hidden = true; });
$("#jobs-clear").addEventListener("click", () => api("/api/jobs/clear", { method: "POST" }).then(pollJobs));
$("#btn-infer-all").addEventListener("click", async () => {
  const missing = S.videos.filter((v) => !v.has_predictions && !v.job).length;
  if (!missing) { alert("Every video already has predictions."); return; }
  if (!confirm(`Queue SLEAP inference for ${missing} videos? They run one at a time (~10–20 min each).`)) return;
  await api("/api/jobs/infer", { method: "POST", body: JSON.stringify({ all_missing: true }) });
  $("#jobs-drawer").hidden = false; pollJobs();
});
$("#btn-analyze").addEventListener("click", async () => {
  const ready = S.videos.filter((v) => v.has_predictions && v.has_bowl).length;
  if (!confirm(`Run the analysis on ${ready} ready videos (videos without predictions or a bowl are skipped)?`)) return;
  const r = await api("/api/jobs/analyze", { method: "POST", body: JSON.stringify({ plots: true }) });
  S.selJob = r.submitted[0]; $("#jobs-drawer").hidden = false; pollJobs();
});

// --------------------------------------------------------------------------- results

// Group colours: the categorical slots used by the figures (feeding/plots.py CAT).
const CAT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"];
const VIEW_FIELDS = ["group", "condition", "context", "chamber", "animal", "session", "part"];
const R = {
  runs: [], run: null, detail: null, tab: "compare", view: null, viewData: null, sub: "bouts",
  feature: null, query: "", sigOnly: false, sort: "order", cfgViews: null, animal: "", kind: "heatmaps", watch: new Set(),
};
const put = (node, ...kids) => node.replaceChildren(...kids.flat().filter((k) => k != null && k !== false));
const runUrl = (f) => `/results/${encodeURIComponent(R.run)}/${f.split("/").map(encodeURIComponent).join("/")}`;
const fmt = (x, d = 3) => (x == null || !isFinite(x) ? "–" : Math.abs(x) >= 1000 || (Math.abs(x) < 0.001 && x !== 0) ? x.toExponential(2) : (+x).toPrecision(d));
const fmtP = (p) => (p == null || !isFinite(p) ? "–" : p < 1e-4 ? p.toExponential(1) : p.toFixed(p < 0.01 ? 4 : 3));
const updateResultsHash = () => history.replaceState(null, "", "#" + ["results", R.run || "", R.tab, R.tab === "compare" ? R.view || "" : ""]
  .map(encodeURIComponent).join("/").replace(/\/+$/, ""));

async function showResults(run, tab, view) {
  togglePlay(false);
  for (const id of ["#empty", "#detail", "#settings", "#welcome", "#help"]) $(id).hidden = true;
  $("#results").hidden = false;
  if (tab) R.tab = tab;
  if (view !== undefined) R.view = view || null;
  try {
    [R.runs, R.cfgViews] = await Promise.all([api("/api/results"), api("/api/views")]);
  } catch (e) {
    const ok = await checkServerVersion();
    put($("#res-body"), el("p", { class: "stale" }, ok ? `Could not load results: ${e.message}`
      : "Results need a newer server: restart `feeding serve` (Ctrl-C, then run it again) and reload this page."));
    return;
  }
  const sel = $("#res-run");
  sel.replaceChildren(...(R.runs.length ? R.runs.map((r) => el("option", { value: r.name },
    `${new Date(r.created_at).toLocaleString()} · ${r.n_videos} videos · ${r.n_bouts} bouts`)) : [el("option", { value: "" }, "No analysis runs yet")]));
  R.run = run && R.runs.some((r) => r.name === run) ? run : R.runs[0]?.name || null;
  sel.value = R.run || "";
  sel.disabled = !R.runs.length;
  await loadRun();
}

async function loadRun() {
  R.detail = R.run ? await api(`/api/results/${encodeURIComponent(R.run)}`) : null;
  const d = R.detail;
  $("#res-meta").textContent = d ? `${d.name}${d.git?.commit ? ` · git ${d.git.commit.slice(0, 8)}${d.git.dirty ? " (uncommitted changes)" : ""}` : ""}` : "";
  $("#res-export").hidden = $("#res-reveal").hidden = !d;
  if (d) $("#res-export").href = `/api/results/${encodeURIComponent(d.name)}/export.zip`;
  if (R.view && !allViews().some((v) => v.slug === R.view)) R.view = null;
  R.view ||= allViews()[0]?.slug || null;
  await renderResults();
}

// Views known to the project (standard first) merged with the views this run has results for.
function allViews() {
  const out = [];
  const ran = new Map((R.detail?.views || []).map((v) => [v.slug, v]));
  const proj = R.cfgViews;
  const slugOf = (name) => name.replace(/[^A-Za-z0-9]+/g, "-").replace(/^-|-$/g, "").toLowerCase() || "view";
  if (proj?.standard) out.push({ slug: "standard", name: proj.standard.name, def: proj.standard, builtin: true, enabled: proj.standard_enabled });
  for (const v of proj?.views || []) out.push({ slug: slugOf(v.name), name: v.name, def: v });
  for (const [slug, v] of ran) if (!out.some((o) => o.slug === slug)) out.push({ slug, name: v.name, def: null, orphan: true });
  for (const o of out) o.ran = ran.get(o.slug) || null;
  return out;
}

async function renderResults() {
  for (const b of document.querySelectorAll("#res-tabs .tab")) b.classList.toggle("active", b.dataset.rtab === R.tab);
  const body = $("#res-body");
  updateResultsHash();
  if (!R.detail && R.tab !== "compare") {
    body.replaceChildren(el("p", { class: "muted" }, "No analysis runs yet. Mark bowls, run SLEAP, then press “Run analysis”."));
    return;
  }
  const notes = (R.detail?.notes || []).map((n) => el("div", { class: "notice" }, n));
  if (R.detail && !R.detail.views.length && !notes.length)  // runs made before comparisons existed, or with one group
    notes.push(el("div", { class: "notice" }, `No comparisons were computed for this run (${R.detail.n_videos} video${R.detail.n_videos === 1 ? "" : "s"} analysed). `,
      "Statistics need animals in at least two groups (Settings → Animals and groups) with analysed videos; per-video numbers are under Videos."));
  if (R.tab === "compare") return renderCompare(body, notes);
  if (R.tab === "videos") return renderVideos(body);
  if (R.tab === "explore") return renderExplore(body);
  if (R.tab === "animals") return renderAnimals(body);
  return renderRunDetails(body);
}

// ---- comparisons tab

async function renderCompare(body, notes = []) {
  const views = allViews();
  const list = el("div", { class: "view-list" },
    ...views.map((v) => el("div", { class: "view-item" + (v.slug === R.view ? " active" : ""), onclick: () => selectView(v.slug) },
      el("div", { class: "vi-name" }, v.name, v.builtin ? el("span", { class: "tag" }, "built-in") : null),
      el("div", { class: "small muted" }, v.ran
        ? (v.ran.error ? el("span", { class: "stale" }, "failed: " + v.ran.error)
          : `${v.ran.n_groups} groups · ${v.ran.n_pairs} pair${v.ran.n_pairs === 1 ? "" : "s"} · ${v.ran.n_sig_bouts + v.ran.n_sig_sessions} significant`)
        : (R.detail ? "not run on this analysis yet" : "saved; runs with the next analysis")),
      v.builtin && !v.enabled ? el("div", { class: "small muted" }, "off for new runs") : null)),
    el("button", { class: "new-view", onclick: () => openViewEditor(null), title: "Compare any groups of animals / sessions" }, "+ New comparison"));
  const opts = el("div", { class: "row small vl-opts" },
    R.cfgViews?.standard ? el("label", { class: "small vl-std" },
      el("input", { type: "checkbox", ...(R.cfgViews.standard_enabled ? { checked: "" } : {}), onchange: (e) => saveViews(R.cfgViews.views, e.target.checked) }),
      " Include the built-in standard comparisons in every run") : null,
    R.detail && views.some((v) => !v.ran && !v.orphan) ? el("button", { class: "ghost small", onclick: () => runViews(null) }, "Run all not yet run on this analysis") : null);
  const main = el("div", { class: "view-main" });
  put(body, notes, list, opts, main);
  await renderView(main);
}

async function selectView(slug) { R.view = slug; R.feature = null; R.viewData = null; await renderResults(); }

async function renderView(main) {
  const v = allViews().find((x) => x.slug === R.view);
  if (!v) { main.replaceChildren(el("p", { class: "muted" }, "Create a comparison to see statistics and figures for any groups you choose.")); return; }
  const head = el("div", { class: "detail-head" },
    el("div", {}, el("h3", { class: "vh" }, v.name), el("div", { class: "small muted" }, v.def?.description || "")),
    el("div", { class: "btn-row" },
      v.def ? el("button", { class: "ghost small", onclick: () => openViewEditor(v) }, v.builtin ? "Copy and edit" : "Edit") : null,
      R.detail && !v.orphan ? el("button", { class: "small", onclick: () => runViews([v.slug]) }, v.ran ? "Re-run" : "Run on this analysis") : null,
      v.ran && !v.ran.error ? el("a", { class: "btnlink small ghostlink", href: runUrl(`views/${v.slug}/stats.xlsx`) }, "Excel") : null,
      v.ran && !v.ran.error ? el("a", { class: "btnlink small ghostlink", href: `/api/results/${encodeURIComponent(R.run)}/export.zip?sub=${encodeURIComponent("views/" + v.slug)}` }, "Download (.zip)") : null));
  if (!v.ran || v.ran.error) {
    put(main, head, groupsTable(v.def, null),
      el("p", { class: "muted" }, !R.detail ? "There is no analysis run yet: this comparison runs with the next “Run analysis”."
        : v.ran?.error ? `This comparison failed: ${v.ran.error}` : "This comparison has not been run on the selected analysis yet."),
      R.watch.size ? el("p", { class: "small muted" }, "Running… results appear here when the job finishes.") : null);
    return;
  }
  if (!R.viewData || R.viewData.slug !== v.slug || R.viewData._run !== R.run) {
    put(main, head, el("p", { class: "muted" }, "Loading…"));
    R.viewData = { ...(await api(`/api/results/${encodeURIComponent(R.run)}/views/${encodeURIComponent(v.slug)}`)), _run: R.run };
  }
  const vd = R.viewData;
  const stale = v.def && JSON.stringify(normView(v.def)) !== JSON.stringify(normView(vd.definition));
  const figs = vd.figures.filter((f) => !f.includes("/boxplots_"));
  put(main, head,
    stale ? el("div", { class: "notice" }, "This comparison has been edited since these results were made. ",
      el("button", { class: "small", onclick: () => runViews([v.slug]) }, "Re-run")) : null,
    vd.groups.filter((g) => !g.animals).map((g) => el("div", { class: "notice" },
      `Group “${g.group}” has no analysed videos in this run (they need SLEAP predictions and a bowl), so its tests are blank.`)),
    groupsTable(vd.definition, vd.groups),
    el("div", { class: "small muted" }, `Statistics: ${vd.analysis.unit === "animal" ? "one value per animal (mean over its bouts)" : "every bout is a sample"}; `,
      `${{ welch: "Welch t-test", student: "Student t-test", mannwhitney: "Mann-Whitney U" }[vd.analysis.test]} (paired: ${vd.analysis.test === "mannwhitney" ? "Wilcoxon" : "paired t-test"}); `,
      `${vd.analysis.correction === "fdr_bh" ? "Benjamini-Hochberg FDR" : "Bonferroni"} across features within each pair. Effect size: Hedges g (paired: d_z), second group minus first.`),
    el("div", { class: "fig-strip" }, ...figs.map((f) => thumb(f, figs))),
    el("nav", { class: "tabs subtabs" },
      ...[["bouts", "Bout features"], ["sessions", "Session & locomotion"]].map(([k, label]) =>
        el("button", { class: "tab" + (R.sub === k ? " active" : ""), onclick: () => { R.sub = k; R.feature = null; renderView(main); } }, label))),
    statsPane(vd));
}

const normView = (d) => d && { n: d.name, g: d.groups.map((g) => [g.label, Object.entries(g.where || {}).sort()]), p: d.pairs || null, q: d.paired || "auto" };

function groupsTable(def, sizes) {
  if (!def) return null;
  const sz = new Map((sizes || []).map((g) => [g.group, g]));
  return el("table", { class: "tbl small groups-tbl" },
    el("tr", {}, el("th", {}, "Group"), el("th", {}, "Matches"), el("th", {}, "Animals"), el("th", {}, "Sessions"), el("th", {}, "Videos"), el("th", {}, "Bouts")),
    ...def.groups.map((g, i) => {
      const s = sz.get(g.label);
      return el("tr", {}, el("td", {}, el("span", { class: "sw", style: `background:${CAT[i] || "#898781"}` }), g.label),
        el("td", { class: "mono" }, Object.entries(g.where || {}).map(([k, vs]) => `${k} = ${vs.join(", ")}`).join("; ") || "everything"),
        el("td", { title: s?.animal_ids || "" }, s ? String(s.animals) : "–"), el("td", {}, s?.sessions != null ? String(s.sessions) : "–"), el("td", {}, s ? String(s.videos) : "–"), el("td", {}, s?.bouts != null ? String(s.bouts) : "–"));
    }));
}

// Per-animal figures are named animal<ID>_<context>.png: [ID, context], or null for other files.
function animalOf(f) {
  const stem = f.split("/").pop().replace(/\.png$/, "");
  const i = stem.lastIndexOf("_");
  return stem.startsWith("animal") && i > 6 ? [stem.slice(6, i), stem.slice(i + 1)] : null;
}
function thumb(f, list) {
  const a = f.startsWith("figures/") && animalOf(f);
  const caption = a ? `Animal ${a[0]} · context ${a[1]}` : f.split("/").pop().replace(".png", "").replace(/_/g, " ");
  return el("figure", { class: "thumb", onclick: () => openLightbox(f, list) },
    el("img", { src: runUrl(f), loading: "lazy", alt: f }), el("figcaption", {}, caption));
}

function statsPane(vd) {
  const rows = vd["stats_" + R.sub] || [];
  const omni = new Map((vd["omnibus_" + R.sub] || []).map((r) => [r.feature, r]));
  if (!rows.length) return el("p", { class: "muted" }, "No data for this comparison.");
  const pairs = [...new Set(rows.map((r) => r.pair))];
  const byFeat = new Map();
  for (const r of rows) (byFeat.get(r.feature) || byFeat.set(r.feature, {}).get(r.feature))[r.pair] = r;
  let feats = [...byFeat.keys()];
  const minP = (f) => Math.min(...Object.values(byFeat.get(f)).map((r) => (isFinite(r.p_adj) ? r.p_adj : 2)));
  if (R.query) feats = feats.filter((f) => f.toLowerCase().includes(R.query.toLowerCase()));
  if (R.sigOnly) feats = feats.filter((f) => minP(f) < 0.05 || (omni.get(f)?.p_adj ?? 1) < 0.05);
  if (R.sort === "p") feats.sort((a, b) => minP(a) - minP(b));
  if (!R.feature || !byFeat.has(R.feature)) R.feature = feats[0] || null;

  const cell = (r) => {
    if (!r) return el("td", {}, "");
    const e = isFinite(r.effect_size) ? Math.max(-1.5, Math.min(1.5, r.effect_size)) / 1.5 : 0;
    const bg = e > 0 ? `rgba(227,73,72,${0.55 * e})` : `rgba(57,135,229,${-0.55 * e})`;
    return el("td", { class: "sc", style: `background:${bg}`, title:
      `${r.a}: mean ${fmt(r.mean_a)} (n=${r.n_a})\n${r.b}: mean ${fmt(r.mean_b)} (n=${r.n_b})\n${r.test}: p=${fmtP(r.pvalue)}, adjusted p=${fmtP(r.p_adj)}\n${r.effect_measure} = ${fmt(r.effect_size, 2)}` },
      r.p_adj < 0.05 ? r.stars : r.p_adj != null && isFinite(r.p_adj) ? "·" : "–");
  };
  const table = el("table", { class: "tbl small stats-tbl" },
    el("thead", {}, el("tr", {}, el("th", {}, "Feature"), omni.size ? el("th", { title: "One test across all groups" }, "All groups") : null,
      ...pairs.map((p) => el("th", { class: "pair-h", title: p }, p.replace(" vs ", "\nvs "))))),
    ...feats.map((f) => el("tr", { class: f === R.feature ? "sel" : "", onclick: () => { R.feature = f; renderView($(".view-main")); } },
      el("td", { class: "mono" }, f),
      omni.size ? el("td", { class: "sc", title: `${omni.get(f)?.test}: adjusted p=${fmtP(omni.get(f)?.p_adj)}` }, omni.get(f)?.p_adj < 0.05 ? omni.get(f).stars : "·") : null,
      ...pairs.map((p) => cell(byFeat.get(f)[p])))));
  const controls = el("div", { class: "row small stats-ctl" },
    el("input", { type: "search", placeholder: "Filter features…", value: R.query, oninput: (e) => { R.query = e.target.value; const pos = e.target.selectionStart; renderView($(".view-main")).then(() => { const i = $(".stats-ctl input[type=search]"); i.focus(); i.setSelectionRange(pos, pos); }); } }),
    el("label", {}, el("input", { type: "checkbox", ...(R.sigOnly ? { checked: "" } : {}), onchange: (e) => { R.sigOnly = e.target.checked; renderView($(".view-main")); } }), " only significant"),
    el("select", { onchange: (e) => { R.sort = e.target.value; renderView($(".view-main")); } },
      el("option", { value: "order", ...(R.sort === "order" ? { selected: "" } : {}) }, "Feature order"),
      el("option", { value: "p", ...(R.sort === "p" ? { selected: "" } : {}) }, "Smallest p first")),
    el("span", { class: "muted" }, `${feats.length} of ${byFeat.size} · cells: stars = adjusted p (· not significant), colour = effect size (red: second group higher)`));
  const f = R.feature;
  const box = vd.figures.find((x) => x.endsWith(`/boxplots_${R.sub}/${f}.png`));
  const detail = f ? el("div", { class: "feat-detail" },
    box ? el("img", { src: runUrl(box), class: "boxfig", onclick: () => openLightbox(box, vd.figures.filter((x) => x.includes(`/boxplots_${R.sub}/`))) }) : el("p", { class: "muted small" }, "No figure (figures were turned off for this run)."),
    el("table", { class: "tbl small" },
      el("tr", {}, ...["Pair", "n", "Means", "Effect", "p (adjusted)"].map((h) => el("th", {}, h))),
      ...pairs.map((p) => byFeat.get(f)[p]).filter(Boolean).map((r) => el("tr", { title: r.test },
        el("td", {}, r.pair), el("td", {}, `${r.n_a}, ${r.n_b}`), el("td", {}, `${fmt(r.mean_a)}, ${fmt(r.mean_b)}`),
        el("td", {}, `${fmt(r.effect_size, 2)} ${r.effect_measure === "d_z" ? "d_z" : "g"}`),
        el("td", { class: r.p_adj < 0.05 ? "sig" : "" }, `${fmtP(r.p_adj)} ${r.stars || ""}`, el("span", { class: "muted" }, ` (${fmtP(r.pvalue)})`))))),
    el("p", { class: "small muted" }, "Hover a row for the test used. p in brackets is unadjusted.")) : null;
  return el("div", {}, controls, el("div", { class: "stats-wrap" }, el("div", { class: "table-wrap stats-scroll" }, table), detail));
}

async function runViews(slugs) {
  const r = await api(`/api/results/${encodeURIComponent(R.run)}/views`, { method: "POST", body: JSON.stringify({ slugs }) });
  for (const id of r.submitted) R.watch.add(id);
  S.selJob = r.submitted[0]; $("#jobs-drawer").hidden = false; pollJobs();
  renderResults();
}

// Called by pollJobs: reload the results page when a job it waits for (or any analysis) finishes.
async function resultsJobsChanged(jobs) {
  if ($("#results").hidden) return;
  let reload = false;
  for (const id of [...R.watch]) {
    const j = jobs.find((x) => x.id === id);
    if (!j || j.status === "done" || j.status === "failed" || j.status === "cancelled") { R.watch.delete(id); reload = true; }
  }
  const finishedAnalyses = jobs.filter((j) => j.kind === "analyze" && j.status === "done").map((j) => j.id).join();
  if (finishedAnalyses !== R.lastAnalyses) { if (R.lastAnalyses !== undefined) reload = true; R.lastAnalyses = finishedAnalyses; }
  if (reload) { R.viewData = null; await showResults(R.watch.size ? R.run : undefined); }
}

// ---- view editor

const VD = { orig: null, groups: [], pairsMode: "all", pairs: new Set() };
const RARE = ["animal", "session", "part"];  // long chip rows, shown on demand
const ready = (v) => v.has_predictions && v.has_bowl;

function openViewEditor(v) {
  const copy = v?.builtin;
  VD.orig = v && !copy ? v : null;
  const def = v?.def ? JSON.parse(JSON.stringify(v.def)) : null;
  $("#vd-title").textContent = VD.orig ? `Edit “${v.name}”` : copy ? "New comparison (copy of the built-in one)" : "New comparison";
  $("#vd-name").value = def ? (copy ? `${def.name} (copy)` : def.name) : "";
  $("#vd-desc").value = def?.description || "";
  VD.groups = def ? def.groups.map((g) => ({ label: g.label, where: g.where || {}, auto: false })) : [{ label: "", where: {}, auto: true }, { label: "", where: {}, auto: true }];
  VD.pairsMode = def?.pairs ? "pick" : "all";
  VD.pairs = new Set((def?.pairs || []).map((p) => JSON.stringify(p)));
  $("#vd-paired").value = def?.paired || "auto";
  $("#vd-unit").textContent = `analysis.unit = ${R.cfgViews?.analysis?.unit || "animal"}`;
  $("#vd-delete").hidden = !VD.orig;
  $("#vd-save-run").hidden = !R.detail;
  const fields = R.cfgViews?.fields || {};
  $("#vd-split").replaceChildren(...VIEW_FIELDS.filter((f) => (fields[f] || []).length > 1).map((f) => el("option", { value: f }, f)));
  vdMsg("");
  renderVD();
  $("#view-dialog").showModal();
}

function vdMsg(t, err = false) { $("#vd-msg").textContent = t; $("#vd-msg").className = "small" + (err ? " stale" : ""); }
const autoLabel = (where) => Object.entries(where).map(([, vs]) => vs.join("+")).join(" ") || "everything";
const matchVideos = (where) => S.videos.filter((v) => Object.entries(where).every(([k, vs]) => vs.includes(String(v[k] ?? ""))));

function renderVD() {
  const fields = R.cfgViews?.fields || {};
  const shown = VIEW_FIELDS.filter((f) => (fields[f] || []).length > 1);
  $("#vd-groups").replaceChildren(...VD.groups.map((g, i) => {
    const vids = matchVideos(g.where);
    const animals = new Set(vids.map((v) => v.animal));
    return el("div", { class: "vd-group" },
      el("div", { class: "row" },
        el("span", { class: "sw", style: `background:${CAT[i] || "#898781"}` }),
        el("input", { type: "text", class: "vd-label", value: g.label, placeholder: autoLabel(g.where),
          oninput: (e) => { g.label = e.target.value; g.auto = !e.target.value; renderPairs(); } }),
        el("span", { class: "small " + (vids.filter(ready).length ? "muted" : "stale"), title: "ready = has SLEAP predictions and a bowl, so it can be analysed" },
          `${animals.size} animals · ${vids.length} videos (${vids.filter(ready).length} ready)`),
        el("span", { class: "spacer" }),
        VD.groups.length > 2 ? el("button", { type: "button", class: "ghost small", title: "Remove group", onclick: () => { VD.groups.splice(i, 1); renderVD(); } }, "×") : null),
      ...shown.filter((f) => !RARE.includes(f) || g.more || g.where[f]).map((f) => el("div", { class: "chips" }, el("span", { class: "chip-f" }, f),
        ...fields[f].map((val) => {
          const on = (g.where[f] || []).includes(val);
          return el("button", { type: "button", class: "chip" + (on ? " on" : ""), onclick: () => {
            const cur = new Set(g.where[f] || []);
            on ? cur.delete(val) : cur.add(val);
            if (cur.size) g.where[f] = [...cur].sort((a, b) => fields[f].indexOf(a) - fields[f].indexOf(b)); else delete g.where[f];
            renderVD();
          } }, val);
        }))),
      shown.some((f) => RARE.includes(f) && !g.where[f]) ? el("button", { type: "button", class: "linklike small more-f",
        onclick: () => { g.more = !g.more; renderVD(); } }, g.more ? "fewer fields" : `more fields (${shown.filter((f) => RARE.includes(f)).join(", ")})`) : null);
  }));
  renderPairs();
}

const labelOf = (g) => g.label.trim() || autoLabel(g.where);

function renderPairs() {
  for (const b of document.querySelectorAll("#vd-pairs-mode button")) b.classList.toggle("on", b.dataset.pm === VD.pairsMode);
  const labels = VD.groups.map(labelOf);
  const all = [];
  for (let i = 0; i < labels.length; i++) for (let j = i + 1; j < labels.length; j++) all.push([labels[i], labels[j]]);
  const box = $("#vd-pairs");
  if (VD.pairsMode === "all") { box.replaceChildren(el("span", { class: "small muted" }, `${all.length} pair${all.length === 1 ? "" : "s"}: every group against every other.`)); return; }
  box.replaceChildren(...all.map((p) => {
    const key = JSON.stringify(p);
    return el("label", { class: "small" }, el("input", { type: "checkbox", ...(VD.pairs.has(key) ? { checked: "" } : {}),
      onchange: (e) => { e.target.checked ? VD.pairs.add(key) : VD.pairs.delete(key); } }), ` ${p[0]}  vs  ${p[1]}`);
  }));
}

function vdCollect() {
  const name = $("#vd-name").value.trim();
  if (!name) throw new Error("Give the comparison a name.");
  const groups = VD.groups.map((g) => ({ label: labelOf(g), where: g.where }));
  const labels = groups.map((g) => g.label);
  if (new Set(labels).size !== labels.length) throw new Error("Two groups have the same label; rename one.");
  for (const g of groups) if (!matchVideos(g.where).length) throw new Error(`Group “${g.label}” matches no videos.`);
  const view = { name, description: $("#vd-desc").value.trim(), groups, paired: $("#vd-paired").value };
  if (VD.pairsMode === "pick") {
    const pairs = [...VD.pairs].map((k) => JSON.parse(k)).filter(([a, b]) => labels.includes(a) && labels.includes(b));
    if (!pairs.length) throw new Error("Tick at least one pair, or choose “All pairs”.");
    view.pairs = pairs;
  }
  return view;
}

async function saveViews(views, standardEnabled) {
  R.cfgViews = await api("/api/views", { method: "PUT", body: JSON.stringify({ views, standard_enabled: standardEnabled }) });
  await renderResults();
}

async function vdSave(run) {
  let view;
  try { view = vdCollect(); } catch (e) { vdMsg(e.message, true); return; }
  const views = (R.cfgViews.views || []).filter((v) => v.name !== VD.orig?.def?.name);
  const slug = view.name.replace(/[^A-Za-z0-9]+/g, "-").replace(/^-|-$/g, "").toLowerCase() || "view";
  if (slug === "standard" || views.some((v) => v.name.replace(/[^A-Za-z0-9]+/g, "-").replace(/^-|-$/g, "").toLowerCase() === slug)) {
    vdMsg("Another comparison already has this name.", true); return;
  }
  const idx = (R.cfgViews.views || []).findIndex((v) => v.name === VD.orig?.def?.name);
  idx >= 0 ? views.splice(idx, 0, view) : views.push(view);
  try { await saveViews(views); } catch (e) { vdMsg(e.message, true); return; }
  $("#view-dialog").close();
  R.view = slug; R.viewData = null;
  if (run && R.detail) await runViews([slug]); else await renderResults();
}

$("#vd-add").addEventListener("click", () => { VD.groups.push({ label: "", where: {}, auto: true }); renderVD(); });
$("#vd-split-go").addEventListener("click", () => {
  const f = $("#vd-split").value;
  if (!f) return;
  // The template is the last group with filters; it is replaced by one group per value
  // (keeping its other filters), and empty placeholder groups are dropped.
  const tIdx = VD.groups.map((g) => Object.keys(g.where).length > 0).lastIndexOf(true);
  const base = { ...(VD.groups[tIdx]?.where || {}) };
  delete base[f];
  const made = R.cfgViews.fields[f].map((val) => ({ label: "", where: { ...base, [f]: [val] }, auto: true }));
  const kept = VD.groups.filter((g, i) => i !== tIdx && (Object.keys(g.where).length || g.label.trim()));
  VD.groups = tIdx >= 0 ? [...kept.slice(0, tIdx), ...made, ...kept.slice(tIdx)] : [...kept, ...made];
  renderVD();
});
for (const b of document.querySelectorAll("#vd-pairs-mode button")) b.addEventListener("click", () => { VD.pairsMode = b.dataset.pm; renderPairs(); });
$("#vd-save").addEventListener("click", () => vdSave(false));
$("#vd-save-run").addEventListener("click", () => vdSave(true));
$("#vd-cancel").addEventListener("click", () => $("#view-dialog").close());
$("#vd-x").addEventListener("click", () => $("#view-dialog").close());
$("#vd-delete").addEventListener("click", async () => {
  if (!confirm(`Delete the comparison “${VD.orig.name}” from the project? Results already computed stay in their run folders.`)) return;
  await saveViews((R.cfgViews.views || []).filter((v) => v.name !== VD.orig.def.name));
  $("#view-dialog").close();
  R.view = null; await renderResults();
});

// ---- other tabs

function gallery(files) { return el("div", { class: "gallery" }, ...files.map((f) => thumb(f, files))); }

function renderExplore(body) {
  const ex = R.detail.exploratory;
  const s = ex.summary || {};
  const facts = [];
  if (s.pca?.explained_variance_ratio) facts.push(["PCA variance explained", s.pca.explained_variance_ratio.map((x) => `${(100 * x).toFixed(0)}%`).join(", ")],
    ["PCA features", (s.pca.features || []).join(", ")]);
  for (const [k, v] of Object.entries(s)) if (k.startsWith("local_clustering") && v?.pvalue != null)
    facts.push([`Local clustering of '${k.slice("local_clustering_".length).replace(/_A_vs_B$/, "")}' bouts, ${v.contexts.join(" vs ")}`, `Welch t = ${fmt(v.statistic)}, p = ${fmtP(v.pvalue)}`]);
  if (s.rf_auc) facts.push([`Random forest held-out AUC (${s.rf_conditions ? s.rf_conditions.join(" vs ") : "first vs last condition"})`, Object.entries(s.rf_auc).map(([c, a]) => `${c}: ${a.toFixed(2)}`).join(", ")]);
  if (s.umap) facts.push(["UMAP", s.umap]);
  const order = (f) => ["pca", "umap", "local", "rf"].findIndex((p) => f.split("/").pop().startsWith(p));
  const figs = [...ex.figures].sort((a, b) => order(a) - order(b));
  const pa = ex.per_animal || [];
  const featOf = (f) => f.split("/").pop().replace(/\.png$/, "");
  if (!R.exFeature || !pa.some((f) => featOf(f) === R.exFeature)) R.exFeature = pa.length ? featOf(pa[0]) : null;
  const cur = pa.find((f) => featOf(f) === R.exFeature);
  put(body,
    pa.length ? el("section", { class: "ex-animals" },
      el("div", { class: "row" }, el("h3", {}, "Each feature per animal"),
        el("select", { onchange: (e) => { R.exFeature = e.target.value; renderExplore(body); } },
          ...pa.map((f) => el("option", { value: featOf(f), ...(featOf(f) === R.exFeature ? { selected: "" } : {}) }, featOf(f)))),
        el("button", { class: "ghost small", onclick: () => { const i = pa.indexOf(cur); R.exFeature = featOf(pa[(i - 1 + pa.length) % pa.length]); renderExplore(body); } }, "←"),
        el("button", { class: "ghost small", onclick: () => { const i = pa.indexOf(cur); R.exFeature = featOf(pa[(i + 1) % pa.length]); renderExplore(body); } }, "→"),
        el("span", { class: "small muted" }, "Each point is a bout; one box per condition (and chamber), sessions in recording order.")),
      cur ? el("img", { class: "ex-animal-fig", src: runUrl(cur), onclick: () => openLightbox(cur, pa) }) : null) : null,
    figs.length ? el("h3", {}, "Projections, clustering and classifier") : null,
    facts.length ? el("dl", { class: "summary facts" }, ...facts.flatMap(([k, v]) => [el("dt", {}, k), el("dd", {}, v)])) : null,
    figs.length ? el("p", { class: "small muted" }, "Bout features of the analysed conditions, standardised; each point is one bout. Tables: ",
      ...["pca.csv", "umap.csv", "rf_conditions.csv", "summary.json"].map((f) => el("a", { href: runUrl("exploratory/" + f), class: "flink" }, f))) : null,
    figs.length ? gallery(figs) : el("p", { class: "muted small" }, "Projections, clustering and the classifier need at least 10 bouts from the compared conditions."));
}

function renderAnimals(body) {
  const pa = R.detail.per_animal;
  const files = pa[R.kind] || [];
  const animals = [...new Set(files.map((f) => animalOf(f)?.[0]).filter(Boolean))].sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
  const shown = R.animal ? files.filter((f) => animalOf(f)?.[0] === R.animal) : files;
  put(body,
    el("div", { class: "row small" },
      el("div", { class: "seg" }, ...[["heatmaps", "Heatmaps"], ["tornado_distance", "Tornado: distance"], ["tornado_speed", "Tornado: speed"]]
        .map(([k, l]) => el("button", { class: R.kind === k ? "on" : "", onclick: () => { R.kind = k; renderAnimals(body); } }, `${l} (${(pa[k] || []).length})`))),
      el("select", { onchange: (e) => { R.animal = e.target.value; renderAnimals(body); } },
        el("option", { value: "" }, "All animals"), ...animals.map((a) => el("option", { value: a, ...(a === R.animal ? { selected: "" } : {}) }, `Animal ${a}`))),
      el("span", { class: "muted" }, "For each animal and context, one panel per session in recording order; bowl-centred.")),
    files.length ? gallery(shown) : el("p", { class: "muted" }, "No per-animal figures in this run."));
}

const SESSION_HIDE = new Set(["session", "part", "chamber", "n_frames", "valid_frames", "in_bowl_frames"]);
function renderVideos(body) {
  const rows = R.detail.sessions || [];
  if (!rows.length) { put(body, el("p", { class: "muted" }, "No videos were analysed in this run.")); return; }
  const cols = Object.keys(rows[0]).filter((c) => !SESSION_HIDE.has(c));
  const ID = new Set(["video", "animal", "group", "condition", "context"]);
  const num = (c) => !ID.has(c) && rows.some((r) => typeof r[c] === "number");
  put(body,
    el("p", { class: "small muted" }, `${rows.length} video${rows.length === 1 ? "" : "s"} analysed (one row each; `,
      el("a", { href: runUrl("sessions.csv"), class: "flink" }, "sessions.csv"), el("a", { href: runUrl("bout_features.csv"), class: "flink" }, "bout_features.csv"),
      "). Click a row to open the video. Locomotion columns use the body node over tracked frames."),
    el("div", { class: "table-wrap videos-wrap" }, el("table", { class: "tbl small" },
      el("thead", {}, el("tr", {}, ...cols.map((c) => el("th", { class: num(c) ? "num" : "" }, c.replace(/_/g, " "))))),
      ...rows.map((r) => el("tr", { class: "clickable", onclick: () => {
        const v = String(r.video).split("+")[0];
        if (S.videos.some((x) => x.video === v)) { $("#results").hidden = true; selectVideo(v, "track"); }
      } }, ...cols.map((c) => el("td", { class: num(c) ? "num" : c === "video" ? "mono" : "" },
        r[c] == null ? "–" : num(c) ? fmt(r[c]) : String(r[c]).replace(/\.0$/, ""))))))));
}

function renderRunDetails(body) {
  const d = R.detail;
  const a = d.analysis || {};
  const skipped = Object.entries(d.skipped || {});
  put(body,
    el("dl", { class: "summary facts" },
      ...[["Folder", d.path], ["Created", new Date(d.created_at).toLocaleString()], ["Took", `${d.elapsed_s} s`],
        ["Videos analysed", d.n_videos], ["Bouts", d.n_bouts], ["Analysis hash", d.analysis_hash],
        ["Statistics", `unit ${a.unit}, ${a.test}, ${a.correction}, seed ${a.seed}`],
        ["Code", d.git?.commit ? `${d.git.commit}${d.git.dirty ? " (uncommitted changes)" : ""}` : "not a git checkout"]]
        .flatMap(([k, v]) => [el("dt", {}, k), el("dd", {}, String(v ?? "–"))])),
    el("h3", {}, "Files"),
    el("p", { class: "small muted" }, "Everything is in the run folder; “Export all” downloads it as one zip."),
    el("ul", { class: "files-list" }, ...d.files.map((f) => el("li", {}, el("a", { href: runUrl(f), class: "flink" }, f)))),
    skipped.length ? el("details", {}, el("summary", {}, `${skipped.length} videos skipped`),
      el("pre", { class: "small" }, skipped.map(([k, v]) => `${k}: ${v}`).join("\n"))) : null);
}

// ---- lightbox

const LB = { list: [], i: 0 };
function openLightbox(f, list) { LB.list = list; LB.i = Math.max(0, list.indexOf(f)); showLB(); $("#lightbox").showModal(); }
function showLB() {
  const f = LB.list[LB.i];
  $("#lb-img").src = runUrl(f); $("#lb-open").href = runUrl(f);
  $("#lb-caption").textContent = `${f}  (${LB.i + 1}/${LB.list.length})`;
}
$("#lb-prev").addEventListener("click", () => { LB.i = (LB.i - 1 + LB.list.length) % LB.list.length; showLB(); });
$("#lb-next").addEventListener("click", () => { LB.i = (LB.i + 1) % LB.list.length; showLB(); });
$("#lb-x").addEventListener("click", () => $("#lightbox").close());
$("#lightbox").addEventListener("click", (e) => { if (e.target.id === "lightbox") $("#lightbox").close(); });
$("#lightbox").addEventListener("keydown", (e) => {
  if (e.key === "ArrowLeft") $("#lb-prev").click();
  if (e.key === "ArrowRight") $("#lb-next").click();
});

$("#btn-results").addEventListener("click", () => showResults());
$("#res-run").addEventListener("change", async (e) => { R.run = e.target.value; R.viewData = null; await loadRun(); });
for (const b of document.querySelectorAll("#res-tabs .tab")) b.addEventListener("click", () => { R.tab = b.dataset.rtab; renderResults(); });
$("#res-reveal").addEventListener("click", async () => {
  const r = await api(`/api/results/${encodeURIComponent(R.run)}/reveal`, { method: "POST" });
  if (!r.ok) alert(`The run folder is:\n${r.path}\n\n(${r.reason})`);
});
$("#results-close").addEventListener("click", () => {
  $("#results").hidden = true;
  if (S.sel) { $("#detail").hidden = false; updateHash(); } else { $("#empty").hidden = false; history.replaceState(null, "", "#"); }
});

// --------------------------------------------------------------------------- boot

window.addEventListener("resize", () => { if (S.sel) { sizeStage(); render(); } });

// --------------------------------------------------------------------------- projects

async function applyProject(p) {
  togglePlay(false);
  S.project = p;
  S.cfg = p;  // node names, skeleton edges, thresholds used by the drawing code
  S.sel = null; S.detail = null; S.chunks.clear(); S.timeline = null;
  $("#project-name").textContent = p.open ? p.name : "none";
  $("#btn-project").title = p.open ? `${p.config_path}\nconfig hash ${p.analysis_hash}` : "Open or create a project";
  for (const id of ["#btn-infer-all", "#btn-analyze", "#btn-results", "#btn-settings"]) $(id).disabled = !p.open;
  const issues = p.open ? p.issues : [];
  $("#issues").hidden = !issues.length;
  $("#issues").replaceChildren(...issues.map((i) => el("div", { class: i.level }, i.message)));
  $("#detail").hidden = true; $("#results").hidden = true; $("#settings").hidden = true; $("#help").hidden = true;
  $("#welcome").hidden = p.open; $("#empty").hidden = !p.open;
  renderRecent($("#welcome-recent"), p.recent);
  if (!p.open) history.replaceState(null, "", "#");
  await refreshVideos();
}

function renderRecent(ul, items) {
  ul.replaceChildren(...(items.length ? items.map((r) => el("li", { onclick: () => openProject(r.path) },
    el("strong", {}, r.name), el("span", { class: "rp" }, r.path))) : [el("li", { class: "muted" }, "No recent projects.")]));
}

async function openProject(path) {
  try {
    const p = await api("/api/project/open", { method: "POST", body: JSON.stringify({ path }) });
    $("#project-dialog").close();
    await applyProject(p);
  } catch (e) { pdMsg(e.message, true); }
}

function pdMsg(text, err = false) { const m = $("#pd-msg"); m.textContent = text || ""; m.className = "small" + (err ? " err" : ""); }

function showProjectDialog(tab = "open") {
  pdMsg("");
  renderRecent($("#dialog-recent"), S.project?.recent || []);
  setProjectTab(tab);
  if (!$("#project-dialog").open) $("#project-dialog").showModal();
  if (tab === "new") { resetNewForm(); $("#new-name").focus(); } else $("#open-path").focus();
}
function setProjectTab(tab) {
  document.querySelectorAll("[data-ptab]").forEach((b) => b.classList.toggle("active", b.dataset.ptab === tab));
  $("#ptab-open").hidden = tab !== "open"; $("#ptab-new").hidden = tab !== "new";
  pdMsg("");
  if (tab === "new") resetNewForm();
}
document.querySelectorAll("[data-ptab]").forEach((b) => b.addEventListener("click", () => setProjectTab(b.dataset.ptab)));
$("#btn-project").addEventListener("click", () => showProjectDialog("open"));
$("#welcome-open").addEventListener("click", () => showProjectDialog("open"));
$("#welcome-new").addEventListener("click", () => showProjectDialog("new"));
$("#welcome-demo").addEventListener("click", async (e) => {
  e.target.disabled = true;
  $("#welcome-msg").textContent = "Creating the demo project (synthetic videos and predictions; about 20 seconds)…";
  try { await applyProject(await api("/api/project/demo", { method: "POST", body: "{}" })); $("#welcome-msg").textContent = ""; }
  catch (err) { $("#welcome-msg").textContent = err.message; }
  e.target.disabled = false;
});
$("#pd-close").addEventListener("click", () => $("#project-dialog").close());
$("#open-go").addEventListener("click", () => openProject($("#open-path").value.trim()));
$("#open-path").addEventListener("keydown", (e) => { if (e.key === "Enter") openProject(e.target.value.trim()); });

// --------------------------------------------------------------------------- folder picker

let fsResolve = null, fsPath = null, fsKind = "any";
const FS_HINTS = {
  project: "Pick a folder that contains feeding.yaml (marked “project”). Double-click a project to open it.",
  videos: "Pick the folder that contains the video files.",
  model: "Pick a SLEAP model folder (contains training_config.json, marked “SLEAP model”).",
  any: "Pick a folder.",
};
// Native OS dialog first (opened by the server on this machine); in-page browser as fallback.
async function nativePick(kind, title, start) {
  try {
    const r = await api("/api/native/pick", { method: "POST", body: JSON.stringify({ kind, title, start }) });
    return r.supported ? { paths: r.paths } : null;
  } catch { return null; }
}

async function pickFolder(kind = "any", start = null, title = "Choose a folder") {
  const native = await nativePick("folder", title, start);
  if (native) return native.paths ? native.paths[0] : null;
  return pickFolderInPage(kind, start, title);
}

function pickFolderInPage(kind = "any", start = null, title = "Choose a folder") {
  fsKind = kind;
  $("#fs-title").textContent = title;
  $("#fs-hint").textContent = FS_HINTS[kind] || "";
  $("#fs-dialog").showModal();
  fsShow(start);
  return new Promise((res) => { fsResolve = res; });
}
function fsDone(value) {
  const r = fsResolve; fsResolve = null;
  if ($("#fs-dialog").open) $("#fs-dialog").close();
  if (r) r(value);
}
async function fsShow(path) {
  try {
    const d = await api(`/api/fs${path ? `?path=${encodeURIComponent(path)}` : ""}`);
    fsPath = d.path;
    $("#fs-path").value = d.path;
    $("#fs-up").disabled = !d.parent;
    $("#fs-up").onclick = () => fsShow(d.parent);
    $("#fs-home").onclick = () => fsShow(d.home);
    const ok = fsKind === "project" ? d.is_project : fsKind === "model" ? d.is_model : fsKind === "videos" ? d.n_videos > 0 : true;
    $("#fs-choose").disabled = !ok;
    $("#fs-choose").textContent = fsKind === "project" ? "Open this project" : "Use this folder" +
      (d.is_model ? " (SLEAP model)" : d.n_videos ? ` (${d.n_videos} videos)` : "");
    $("#fs-list").replaceChildren(...(d.entries.length ? d.entries.map((e) => el("li", {
      onclick: () => fsShow(e.path),
      ondblclick: () => { if ((fsKind === "project" && e.is_project) || (fsKind === "model" && e.is_model)) fsDone(e.path); },
    }, el("span", {}, `📁 ${e.name}`),
      el("span", { class: "tag" + (e.is_project ? " proj" : e.is_model ? " model" : "") },
        e.is_project ? "project" : e.is_model ? "SLEAP model" : e.n_videos ? `${e.n_videos} videos` : ""))) : [el("li", { class: "muted" }, "No sub-folders.")]));
  } catch (e) {
    $("#fs-hint").textContent = `Cannot list folder: ${e.message}`;
  }
}
$("#fs-path").addEventListener("keydown", (e) => { if (e.key === "Enter") fsShow(e.target.value.trim()); });
$("#fs-choose").addEventListener("click", () => fsDone(fsPath));
$("#fs-cancel").addEventListener("click", () => fsDone(null));
$("#fs-x").addEventListener("click", () => fsDone(null));
$("#fs-dialog").addEventListener("cancel", () => fsDone(null));

$("#ptab-open [data-browse]").addEventListener("click", async () => {
  const p = await pickFolder("project", $("#open-path").value.trim() || null, "Open a project");
  if (p) { $("#open-path").value = p; openProject(p); }
});

// --------------------------------------------------------------------------- uploads

function uploadVideo(file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", `/api/project/videos/upload?name=${encodeURIComponent(file.name)}`);
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(e.loaded / e.total); };
    xhr.onload = () => (xhr.status < 300 ? resolve(JSON.parse(xhr.responseText)) : reject(new Error(xhr.responseText || xhr.statusText)));
    xhr.onerror = () => reject(new Error("network error"));
    xhr.send(file);
  });
}

async function uploadAll(files, show) {
  const total = files.reduce((a, f) => a + f.size, 0) || 1;
  let done = 0;
  const results = [];
  for (const [i, f] of files.entries()) {
    try {
      results.push(await uploadVideo(f, (frac) => show((done + frac * f.size) / total, `Uploading ${i + 1}/${files.length}: ${f.name}`)));
    } catch (e) {
      results.push({ file: f.name, error: e.message });
    }
    done += f.size;
  }
  show(1, "");
  return results;
}

const fmtMB = (b) => `${(b / 1e6).toFixed(b > 1e8 ? 0 : 1)} MB`;

// --------------------------------------------------------------------------- new project

const NP = { files: [], paths: [], folder: null, otherModel: null };

async function resetNewForm() {
  const d = S.project?.defaults || {};
  NP.files = []; NP.paths = []; NP.folder = null; NP.otherModel = null;
  $("#new-files").value = "";
  updateNewSummary();
  $("#new-location").value = "";
  updateLocationHint();
  const models = await api("/api/models").catch(() => []);
  $("#new-model").replaceChildren(el("option", { value: "" }, "None for now"),
    ...models.map((m) => el("option", { value: m.path }, `${m.name}  (${m.nodes.length} nodes)`)));
  $("#new-progress").hidden = true;
  $("#new-go").disabled = false;
  void d;
}
function updateLocationHint() {
  const name = $("#new-name").value.trim();
  const slugged = name.replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "") || "<name>";
  $("#new-location-hint").textContent = $("#new-location").value || `${S.project?.defaults?.projects_dir || "projects"}/${slugged}`;
}
function updateNewSummary() {
  const parts = [];
  if (NP.files.length) parts.push(`${NP.files.length} file${NP.files.length > 1 ? "s" : ""} to upload (${fmtMB(NP.files.reduce((a, f) => a + f.size, 0))})`);
  if (NP.paths.length) parts.push(`${NP.paths.length} file${NP.paths.length > 1 ? "s" : ""} to copy: ${NP.paths.map((p) => p.split("/").pop()).join(", ")}`);
  if (NP.folder) parts.push(`folder ${NP.folder}`);
  $("#new-videos-summary").textContent = parts.join(" + ") || "No videos chosen yet.";
}
$("#new-name").addEventListener("input", updateLocationHint);
$("#new-location-change").addEventListener("click", async () => {
  const p = await pickFolder("any", null, "Where should the project folder go?");
  if (p) {
    const name = $("#new-name").value.trim().replace(/[^A-Za-z0-9._-]+/g, "-") || "project";
    $("#new-location").value = `${p}/${name}`;
    updateLocationHint();
  }
});
$("#new-files").addEventListener("change", (e) => { NP.files = [...e.target.files]; updateNewSummary(); });
$("#new-choose-files").addEventListener("click", async () => {
  const native = await nativePick("videos", "Choose video files", null);
  if (!native) { $("#new-files").click(); return; }  // browser upload instead
  if (native.paths) { NP.paths = native.paths; updateNewSummary(); }
});
$("#ptab-new [data-browse='new-videos-folder']").addEventListener("click", async () => {
  const p = await pickFolder("videos", null, "Folder of videos (used in place)");
  if (p) { NP.folder = p; updateNewSummary(); }
});
$("#ptab-new [data-browse='new-model-other']").addEventListener("click", async () => {
  const p = await pickFolder("model", null, "Choose a SLEAP model folder");
  if (p) {
    $("#new-model").append(el("option", { value: p }, p.split("/").pop()));
    $("#new-model").value = p;
  }
});

$("#new-go").addEventListener("click", async () => {
  const name = $("#new-name").value.trim();
  if (!name) { pdMsg("Give the project a name.", true); return; }
  if (!NP.files.length && !NP.paths.length && !NP.folder) { pdMsg("Choose at least one video (files or a folder).", true); return; }
  $("#new-go").disabled = true;
  pdMsg("Creating project…");
  try {
    const p = await api("/api/project/new", { method: "POST", body: JSON.stringify({
      name, location: $("#new-location").value || null, model: $("#new-model").value || null,
      videos_folder: NP.folder, video_files: NP.paths, video_names: NP.files.map((f) => f.name),
    }) });
    let notes = p.notes || [];
    if (NP.files.length) {
      $("#new-progress").hidden = false;
      const res = await uploadAll(NP.files, (frac, text) => { $("#new-progress i").style.width = `${frac * 100}%`; pdMsg(text); });
      const failed = res.filter((r) => r.error);
      if (failed.length) notes = [...notes, ...failed.map((r) => `upload failed: ${r.file}: ${r.error}`)];
    }
    await applyProject(await api("/api/project"));
    pdMsg(["Project created.", ...notes].join("\n"));
    setTimeout(() => { if ($("#project-dialog").open) $("#project-dialog").close(); }, notes.length ? 5000 : 900);
  } catch (e) { pdMsg(e.message, true); $("#new-go").disabled = false; }
});

// --------------------------------------------------------------------------- settings view

const ST = { data: null, modelRows: [] };

async function showSettings() {
  togglePlay(false);
  for (const id of ["#empty", "#detail", "#results", "#welcome", "#help"]) $(id).hidden = true;
  $("#settings").hidden = false;
  setMsg("");
  ST.data = await api("/api/project/settings");
  renderSettings();
}
function setMsg(text, err = false) { $("#set-msg").textContent = text; $("#set-msg").className = "small" + (err ? " stale" : ""); }

function renderSettings() {
  const d = ST.data;
  $("#set-root").textContent = d.root;
  $("#set-library").textContent = (S.project?.defaults?.model_library || []).join(", ");
  // video folders
  $("#set-dirs").replaceChildren(...d.video_dirs.map((v) => el("li", { class: v.exists ? "" : "missing" },
    el("span", {}, v.path + (v.local ? "  (project videos/)" : "") + (v.exists ? "" : "  — not found")),
    v.local ? null : el("button", { class: "ghost small danger", title: "Stop using this folder", onclick: () => {
      d.video_dirs = d.video_dirs.filter((x) => x !== v); renderSettings(); setMsg("Unsaved changes.");
    } }, "×"))));
  // models
  const rows = Object.entries(d.models).flatMap(([ctx, paths]) => paths.map((p) => ({ ctx: ctx === "default" ? "" : ctx, path: p })));
  ST.modelRows = rows.length ? rows : [{ ctx: "", path: "" }];
  renderModelRows();
  $("#set-skeleton").textContent = d.skeleton.length ? `Skeleton: ${d.skeleton.join(", ")}` : "No skeleton yet (assign a model).";
  $("#set-sleapbin").value = d.sleap_bin;
  ST.sleap = { ...d.sleap_params };
  renderSleapParams();
  // naming
  $("#set-preset").replaceChildren(el("option", { value: "" }, "Presets…"),
    ...Object.entries(d.presets).map(([label, pat]) => el("option", { value: pat }, label)));
  $("#set-pattern").value = d.pattern;
  $("#set-chambers").value = Object.entries(d.chambers).map(([k, v]) => `${k}=${v}`).join(", ");
  $("#set-conditions").value = d.conditions.join(", ");
  previewNaming();
  // subjects
  renderSubjects(d.subjects);
}

function renderModelRows() {
  const opts = ST.data.available_models;
  for (const r of ST.modelRows) {  // saved paths are resolved; show the library entry that points there
    const m = opts.find((o) => o.resolved === r.path || o.path === r.path);
    if (m) r.path = m.path;
  }
  $("#set-models").replaceChildren(...ST.modelRows.map((r, i) => {
    const sel = el("select", { onchange: (e) => { r.path = e.target.value; } },
      el("option", { value: "" }, "— choose a model —"),
      ...opts.map((m) => el("option", { value: m.path }, `${m.name}${m.library.endsWith("/models") && m.path.startsWith(ST.data.root) ? " (project)" : ""}`)),
      ...(r.path && !opts.some((m) => m.path === r.path) ? [el("option", { value: r.path }, r.path)] : []));
    sel.value = r.path;
    return el("div", { class: "model-row" },
      el("input", { type: "text", class: "ctx", placeholder: "context", value: r.ctx, title: "Video context; blank = all videos",
        oninput: (e) => { r.ctx = e.target.value; } }),
      sel,
      el("button", { type: "button", class: "ghost", onclick: async () => {
        const p = await pickFolder("model", null, "Choose a SLEAP model folder");
        if (p) { r.path = p; renderModelRows(); }
      } }, "Other…"),
      el("button", { type: "button", class: "ghost", title: "Copy this model into the project's models/ folder", onclick: async () => {
        if (!r.path) return;
        setMsg("Copying model into the project…");
        try { const res = await api("/api/project/models/import", { method: "POST", body: JSON.stringify({ path: r.path }) });
          ST.data.available_models = res.available_models; r.path = res.path; renderModelRows(); setMsg("Model copied; save to use it.");
        } catch (e) { setMsg(e.message, true); }
      } }, "Copy in"),
      el("button", { type: "button", class: "ghost danger", title: "Remove", onclick: () => { ST.modelRows.splice(i, 1); renderModelRows(); } }, "×"));
  }));
  const ctxs = ST.data.contexts.filter((c) => c !== "NA");
  $("#set-models").append(el("div", { class: "small muted" }, ctxs.length ? `Contexts in your video names: ${ctxs.join(", ")}` : ""));
}
$("#set-add-model").addEventListener("click", () => { ST.modelRows.push({ ctx: "", path: "" }); renderModelRows(); });

// SLEAP inference parameters: one input per parameter, built from the server's description.
function renderSleapParams() {
  const d = ST.data;
  $("#set-sleap-found").textContent = d.sleap_found ? "sleap-track found." :
    "sleap-track not found here: SLEAP inference won't run (importing predictions still works).";
  $("#set-sleap-found").className = "small" + (d.sleap_found ? " muted" : " stale");
  const input = (p) => {
    const v = ST.sleap[p.name], changed = () => { setMsg("Unsaved changes."); sleapCmd(); };
    let ctl;
    if (p.kind === "bool") {
      ctl = el("input", { type: "checkbox", onchange: (e) => { ST.sleap[p.name] = e.target.checked; changed(); } });
      ctl.checked = !!v;
      return el("label", { class: "field check", title: p.help }, ctl, p.label);
    }
    if (p.kind === "choice") {
      ctl = el("select", { onchange: (e) => { ST.sleap[p.name] = e.target.value; changed(); renderSleapParams(); } },
        ...p.choices.map((c) => el("option", { value: c }, c)));
      ctl.value = v;
    } else if (p.kind === "int" || p.kind === "float") {
      ctl = el("input", { type: "number", step: p.kind === "int" ? "1" : "0.05", min: "0", value: v,
        oninput: (e) => { const x = e.target.value === "" ? p.default : +e.target.value; ST.sleap[p.name] = x; changed(); } });
    } else if (p.kind === "list") {
      ctl = el("input", { type: "text", class: "mono", value: (v || []).join(" "), placeholder: "e.g. --frames 0-999",
        oninput: (e) => { ST.sleap[p.name] = e.target.value.trim() ? e.target.value.trim().split(/\s+/) : []; changed(); } });
    } else {
      ctl = el("input", { type: "text", value: v ?? "", oninput: (e) => { ST.sleap[p.name] = e.target.value.trim(); changed(); } });
    }
    return el("label", { class: "field", title: p.help }, el("span", {}, p.label,
      v !== p.default && JSON.stringify(v) !== JSON.stringify(p.default) ? el("span", { class: "muted small" }, ` (default ${Array.isArray(p.default) ? "none" : p.default})`) : null), ctl);
  };
  const info = d.sleap_param_info;
  put($("#set-sleap-params"), info.filter((p) => !p.tracking).map(input));
  put($("#set-sleap-tracking-params"), info.filter((p) => p.tracking).map(input));
  $("#set-sleap-tracking").open = ST.sleap.tracker !== "none";
  sleapCmd();
}
function sleapCmd() {
  const s = ST.sleap, a = ["sleap-track VIDEO -m MODEL", `--batch_size ${s.batch_size}`, `--peak_threshold ${s.peak_threshold}`,
    `--tracking.tracker ${s.tracker}`];
  if (s.device === "cpu") a.push("--cpu"); else if (s.device === "gpu") a.push("--first-gpu"); else if (/^\d+$/.test(s.device)) a.push(`--gpu ${s.device}`);
  if (s.tracker !== "none") a.push(`--tracking.target_instance_count ${s.target_instance_count}`, `--tracking.similarity ${s.similarity}`,
    `--tracking.match ${s.match}`, `--tracking.track_window ${s.track_window}`, "…");
  $("#set-sleap-cmd").textContent = [...a, ...(s.extra_args || [])].join(" ");
}
$("#set-sleapbin-browse").addEventListener("click", async () => {
  const p = await pickFolder("any", null, "SLEAP environment's bin folder (contains sleap-track)");
  if (p) { $("#set-sleapbin").value = p; setMsg("Unsaved changes."); }
});

let previewTimer = null;
function previewNaming() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(async () => {
    const pattern = $("#set-pattern").value;
    try {
      const r = await api(`/api/project/naming-preview?pattern=${encodeURIComponent(pattern)}`);
      if (r.error) { $("#set-preview-summary").textContent = `Invalid pattern: ${r.error}`; $("#set-preview").replaceChildren(); return; }
      $("#set-preview-summary").textContent = `${r.matched} of ${r.total} videos match` + (r.matched < r.total ? " (the rest are ignored)" : "");
      const cols = ["file", "animal", "condition", "context", "session", "part"];
      $("#set-preview").replaceChildren(el("tr", {}, ...cols.map((c) => el("th", {}, c))),
        ...r.rows.map((row) => el("tr", {}, ...cols.map((c) => el("td", { class: !row.ok && c !== "file" ? "bad" : "" },
          c === "file" ? row.file : row.ok ? (row[c] ?? "") : c === "animal" ? "no match" : "")))));
    } catch (e) { $("#set-preview-summary").textContent = e.message; }
  }, 250);
}
$("#set-pattern").addEventListener("input", () => { previewNaming(); setMsg("Unsaved changes."); });
$("#set-preset").addEventListener("change", (e) => { if (e.target.value) { $("#set-pattern").value = e.target.value; previewNaming(); setMsg("Unsaved changes."); } e.target.value = ""; });

function renderSubjects(rows) {
  const groups = [...new Set(rows.map((r) => r.group).filter(Boolean))];
  $("#group-options").replaceChildren(...groups.map((g) => el("option", { value: g })));
  $("#set-subjects").replaceChildren(el("tr", {}, el("th", {}, "animal"), el("th", {}, "group"), el("th", {}, "")),
    ...rows.map((r) => el("tr", {}, el("td", { class: "mono" }, r.animal),
      el("td", {}, el("input", { type: "text", list: "group-options", value: r.group, "data-animal": r.animal,
        oninput: () => setMsg("Unsaved changes.") })),
      el("td", { class: "muted" }, r.has_videos ? "" : "no videos"))));
}

$("#set-save").addEventListener("click", async () => {
  const d = ST.data;
  const models = {};
  for (const r of ST.modelRows) if (r.path) (models[r.ctx.trim() || "default"] ||= []).push(r.path);
  const body = {
    pattern: $("#set-pattern").value,
    chambers: Object.fromEntries($("#set-chambers").value.split(",").map((x) => x.split("=").map((y) => y.trim())).filter((kv) => kv.length === 2 && kv[0] && kv[1])),
    conditions: $("#set-conditions").value.split(",").map((x) => x.trim()).filter(Boolean),
    sleap_bin: $("#set-sleapbin").value.trim(),
    sleap_params: ST.sleap,
    video_dirs: d.video_dirs.map((v) => v.path),
    models,
    subjects: [...document.querySelectorAll("#set-subjects input")].map((i) => ({ animal: i.dataset.animal, group: i.value.trim() })),
  };
  setMsg("Saving…");
  try {
    const res = await api("/api/project/settings", { method: "PUT", body: JSON.stringify(body) });
    ST.data = res; renderSettings();
    setMsg(["Saved.", ...(res.notes || [])].join(" "));
    const p = await api("/api/project");
    S.project = p; S.cfg = p;
    $("#issues").hidden = !p.issues.length;
    $("#issues").replaceChildren(...p.issues.map((i) => el("div", { class: i.level }, i.message)));
    await refreshVideos();
  } catch (e) { setMsg(e.message, true); }
});
$("#set-close").addEventListener("click", () => {
  $("#settings").hidden = true;
  if (S.sel && S.detail) $("#detail").hidden = false; else $("#empty").hidden = false;
});
$("#btn-settings").addEventListener("click", showSettings);
document.querySelector("[data-browse-main='folder']").addEventListener("click", async () => {
  const p = await pickFolder("videos", null, "Folder of videos (used in place)");
  if (!p) return;
  try { ST.data = await api("/api/project/videos/folder", { method: "POST", body: JSON.stringify({ path: p }) }); renderSettings();
    setMsg("Folder added."); await refreshVideos();
  } catch (e) { setMsg(e.message, true); }
});

async function settingsUpload(files) {
  if (!files.length) return;
  const box = $("#set-upload");
  const res = await uploadAll(files, (frac, text) => { box.textContent = text ? `${text} — ${Math.round(frac * 100)}%` : ""; });
  const ok = res.filter((r) => !r.error), bad = res.filter((r) => r.error), off = ok.filter((r) => !r.matches_pattern);
  box.textContent = `${ok.length} uploaded` + (bad.length ? `; ${bad.length} failed (${bad.map((r) => r.file).join(", ")})` : "") +
    (off.length ? `; ${off.length} don't match the naming pattern — adjust it below` : "");
  ST.data = await api("/api/project/settings"); renderSettings();
  await refreshVideos();
}
$("#set-files").addEventListener("change", (e) => { settingsUpload([...e.target.files]); e.target.value = ""; });
$("#set-choose-files").addEventListener("click", async () => {
  const native = await nativePick("videos", "Add video files to the project", null);
  if (!native) { $("#set-files").click(); return; }
  if (!native.paths) return;
  setMsg(`Copying ${native.paths.length} video(s) into the project…`);
  try {
    ST.data = await api("/api/project/videos/add", { method: "POST", body: JSON.stringify({ paths: native.paths }) });
    renderSettings(); setMsg(`Added ${native.paths.length} video(s).`); await refreshVideos();
  } catch (e) { setMsg(e.message, true); }
});
const dz = $("#set-dropzone");
dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("over"); });
dz.addEventListener("dragleave", () => dz.classList.remove("over"));
dz.addEventListener("drop", (e) => { e.preventDefault(); dz.classList.remove("over"); settingsUpload([...e.dataTransfer.files]); });

// --------------------------------------------------------------------------- help (the user manual)

const HELP = { chapters: null, slug: null, back: null };
async function showHelp(slug, anchor) {
  togglePlay(false);
  if ($("#help").hidden) HELP.back = ["#detail", "#results", "#settings", "#empty", "#welcome"].find((id) => !$(id).hidden) || null;
  for (const id of ["#empty", "#detail", "#settings", "#results", "#welcome"]) $(id).hidden = true;
  $("#help").hidden = false;
  try {
    HELP.chapters ||= await api("/api/docs");
    slug = slug || HELP.slug || HELP.chapters[0]?.slug;
    if (!slug) { $("#help-body").textContent = "The manual is not installed (no docs/manual folder)."; return; }
    if (slug !== HELP.slug) {
      const d = await api(`/api/docs/${encodeURIComponent(slug)}`);
      HELP.slug = slug;
      const i = HELP.chapters.findIndex((c) => c.slug === slug), prev = HELP.chapters[i - 1], next = HELP.chapters[i + 1];
      $("#help-body").innerHTML = d.html;
      $("#help-body").append(el("nav", { class: "doc-nav" },
        prev ? el("a", { href: `#help/${prev.slug}` }, `← ${prev.title}`) : el("span"),
        next ? el("a", { href: `#help/${next.slug}` }, `${next.title} →`) : el("span")));
    }
  } catch (e) { $("#help-body").textContent = e.message; return; }
  renderHelpToc(anchor);
  const target = anchor && document.getElementById(anchor);
  if (target) target.scrollIntoView({ block: "start" }); else $("#main").scrollTop = 0;
  history.replaceState(null, "", `#help/${HELP.slug}${anchor ? "/" + anchor : ""}`);
}
function renderHelpToc(anchor) {
  put($("#help-toc"), HELP.chapters.map((c) => el("li", { class: c.slug === HELP.slug ? "active" : "" },
    el("a", { href: `#help/${c.slug}` }, c.title),
    c.slug === HELP.slug && c.toc.some((t) => t.level === 2)
      ? el("ol", {}, ...c.toc.filter((t) => t.level === 2).map((t) => el("li", {}, el("a", { href: `#help/${c.slug}/${t.id}` }, t.text))))
      : null)));
}
function closeHelp() {
  $("#help").hidden = true;
  const back = HELP.back && !(HELP.back === "#welcome" && S.project?.open) ? HELP.back : (S.project?.open ? (S.sel && S.detail ? "#detail" : "#empty") : "#welcome");
  $(back).hidden = false;
  if (back === "#detail") updateHash(); else if (back === "#results") updateResultsHash(); else history.replaceState(null, "", "#");
}
function routeHelp(hash) {
  const [, slug, anchor] = decodeURIComponent(hash.replace(/^#/, "")).split("/");
  showHelp(slug || undefined, anchor || undefined);
}
$("#btn-help").addEventListener("click", () => showHelp());
$("#help-close").addEventListener("click", closeHelp);
window.addEventListener("hashchange", () => { if (location.hash.startsWith("#help")) routeHelp(location.hash); });
$("#help-body").addEventListener("click", (e) => {
  const a = e.target.closest("a"), img = e.target.closest("img");
  if (img && !a) { window.open(img.src, "_blank"); return; }
  if (!a) return;
  const href = a.getAttribute("href") || "";
  if (href.startsWith("#help/")) { e.preventDefault(); routeHelp(href); }
  else if (href.startsWith("#")) { e.preventDefault(); showHelp(HELP.slug, href.slice(1)); }
});
let helpTimer = null;
$("#help-search").addEventListener("input", (e) => {
  clearTimeout(helpTimer);
  const q = e.target.value.trim();
  helpTimer = setTimeout(async () => {
    const ol = $("#help-results");
    if (!q) { ol.hidden = true; $("#help-toc").hidden = false; return; }
    const hits = await api(`/api/docs/search?q=${encodeURIComponent(q)}`);
    ol.hidden = false; $("#help-toc").hidden = true;
    put(ol, hits.length ? hits.map((h) => el("li", { onclick: () => showHelp(h.slug, h.anchor || undefined) },
      el("div", {}, el("strong", {}, h.section), h.section !== h.chapter ? el("span", { class: "muted small" }, ` · ${h.chapter}`) : null),
      el("div", { class: "snip" }, h.snippet))) : [el("li", { class: "muted" }, "Nothing found.")]);
  }, 200);
});

// The page files are read from disk on every load, but the server's Python code only on start:
// after an update, an old server lacks endpoints the new page uses. Say so plainly.
async function checkServerVersion() {
  let stale;
  try { stale = (await api("/api/version")).stale; } catch { stale = true; }
  if (!stale) return true;
  $("#issues").hidden = false;
  $("#issues").prepend(el("div", { class: "error" },
    "The app was updated after this server started. Stop `feeding serve` (Ctrl-C in its terminal) and start it again, then reload this page."));
  return false;
}

(async function boot() {
  const hash = decodeURIComponent(location.hash.slice(1));
  let project;
  try { project = await api("/api/project"); } catch (e) {
    $("#issues").hidden = false;
    $("#issues").replaceChildren(el("div", { class: "error" },
      "This page is newer than the running server (it has no project support). Stop `feeding serve` (Ctrl-C) and start it again."));
    return;
  }
  await applyProject(project);
  checkServerVersion();
  await pollJobs();
  if (hash.startsWith("help")) { routeHelp(hash); return; }
  if (hash === "new-project" || hash === "open-project") {
    showProjectDialog(hash === "new-project" ? "new" : "open");
    return;
  }
  if (!S.project.open) return;
  if (hash.startsWith("results")) {
    const [, run, rtab, view] = hash.split("/");
    showResults(run || undefined, rtab || undefined, view || undefined);
    return;
  }
  const [name, tab, frame] = hash.split("/");
  if (name && S.videos.some((v) => v.video === name)) selectVideo(name, tab, frame ? parseInt(frame, 10) : undefined);
})();
