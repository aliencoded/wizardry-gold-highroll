const { invoke } = window.__TAURI__.core;

const $ = (id) => document.getElementById(id);
const state = { exe: "", factor: 1 };

function log(msg) {
  const el = $("log");
  el.hidden = false;
  el.textContent = msg;
}

function clampInt(v, lo, hi, dflt) {
  v = parseInt(v, 10);
  if (isNaN(v)) return dflt;
  return Math.max(lo, Math.min(hi, v));
}

// --- status rendering -------------------------------------------------------
function setBadge(id, cls, tag, line) {
  const b = $(id);
  b.className = "badge " + cls;
  $(id === "rollBadge" ? "rollTag" : "speedTag").textContent = tag;
  $(id === "rollBadge" ? "rollLine" : "speedLine").textContent = line;
}

function renderRollBar() {
  let lo = clampInt($("lo").value, 0, 40, 21);
  let hi = clampInt($("hi").value, 0, 40, 30);
  if (lo > hi) [lo, hi] = [hi, lo];
  const max = 40;
  const left = (lo / max) * 100;
  const width = ((hi + 1 - lo) / max) * 100;
  $("rollFill").style.left = left + "%";
  $("rollFill").style.width = width + "%";
  $("rollPreview").textContent = `rolls ${lo}-${hi}`;
  $("die").textContent = String(Math.min(40, hi));
}

function selectFactor(f) {
  state.factor = f;
  document.querySelectorAll("#factorSeg button").forEach((b) => {
    b.classList.toggle("on", parseInt(b.dataset.f, 10) === f);
  });
}

async function refresh() {
  state.exe = $("exe").value.trim();
  if (!state.exe) {
    setBadge("rollBadge", "bad", "NO GAME", "paste the path to WIZARD_720.EXE");
    setBadge("speedBadge", "bad", "—", "");
    return;
  }
  let s;
  try {
    s = await invoke("status", { exe: state.exe });
  } catch (e) {
    setBadge("rollBadge", "bad", "ERROR", String(e));
    setBadge("speedBadge", "bad", "—", "");
    return;
  }
  // Roll badge
  const r = s.roll;
  if (r.health === "patched") {
    setBadge("rollBadge", "good", "PATCHED", `forcing rolls ${r.lo}-${r.hi}`);
    if (r.lo != null) { $("lo").value = r.lo; $("hi").value = r.hi; }
  } else if (r.health === "vanilla") {
    setBadge("rollBadge", "warn", "VANILLA", "normal rolls ~5-11");
  } else {
    setBadge("rollBadge", "bad", "BAD BUILD", r.line);
  }
  // Speed badge
  const sp = s.speed;
  if (!sp.found) {
    setBadge("speedBadge", "bad", "SPEED ?", "signature not found");
  } else {
    const fac = sp.factor != null ? `${sp.factor}x` : "?x";
    const tier = (m) => (m != null ? `${m}` : "50");
    const txt = `main ${tier(sp.text_full_ms)} / quick ${tier(sp.text_half_ms)}`;
    const stock = sp.factor === 1 && sp.search_ms === 33
      && sp.text_full_ms == null && sp.text_half_ms == null;
    setBadge("speedBadge", stock ? "warn" : "good", stock ? "STOCK" : "SPEED",
      `${fac} · search ${sp.search_ms}ms · text ${txt}ms`);
    // seed controls
    selectFactor(sp.factor || 1);
    $("search").value = sp.search_ms;
    if (sp.text_full_ms != null) $("textFull").value = sp.text_full_ms;
    if (sp.text_half_ms != null) $("textHalf").value = sp.text_half_ms;
  }
  renderRollBar();
}

// --- actions ----------------------------------------------------------------
async function detect() {
  const p = await invoke("detect");
  if (p) { $("exe").value = p; log("Detected game."); await refresh(); }
  else log("Could not auto-detect — paste the path.");
}

async function doApplyRoll() {
  const lo = clampInt($("lo").value, 0, 40, 21);
  const hi = clampInt($("hi").value, 0, 40, 30);
  log(await invoke("apply_roll", { exe: state.exe, lo, hi }));
  await refresh();
}
async function doRevertRoll() {
  log(await invoke("revert_roll", { exe: state.exe }));
  await refresh();
}
async function doApplySpeed() {
  const search = clampInt($("search").value, 0, 127, 12);
  const textFull = clampInt($("textFull").value, 1, 32767, 1000);
  const textHalf = clampInt($("textHalf").value, 1, 32767, 500);
  log(await invoke("apply_speed", { exe: state.exe, factor: state.factor, search, textFull, textHalf }));
  await refresh();
}
async function doRevertSpeed() {
  log(await invoke("revert_speed", { exe: state.exe }));
  await refresh();
}

// --- wiring -----------------------------------------------------------------
window.addEventListener("DOMContentLoaded", async () => {
  $("detect").addEventListener("click", detect);
  $("refresh").addEventListener("click", refresh);
  $("exe").addEventListener("change", refresh);
  $("lo").addEventListener("input", renderRollBar);
  $("hi").addEventListener("input", renderRollBar);

  document.querySelectorAll(".chip[data-lo]").forEach((b) =>
    b.addEventListener("click", () => { $("lo").value = b.dataset.lo; $("hi").value = b.dataset.hi; renderRollBar(); }));
  document.querySelectorAll(".chip[data-full]").forEach((b) =>
    b.addEventListener("click", () => { $("textFull").value = b.dataset.full; $("textHalf").value = b.dataset.half; }));
  document.querySelectorAll("#factorSeg button").forEach((b) =>
    b.addEventListener("click", () => selectFactor(parseInt(b.dataset.f, 10))));

  $("applyRoll").addEventListener("click", doApplyRoll);
  $("revertRoll").addEventListener("click", doRevertRoll);
  $("applySpeed").addEventListener("click", doApplySpeed);
  $("revertSpeed").addEventListener("click", doRevertSpeed);

  selectFactor(1);
  renderRollBar();
  // Auto-detect on first launch.
  const p = await invoke("detect");
  if (p) { $("exe").value = p; }
  await refresh();
});
