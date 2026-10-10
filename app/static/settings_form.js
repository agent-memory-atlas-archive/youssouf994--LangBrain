/* Settings form shared by the pages that edit configuration files (/config and the pages of the nodes).
   Inserted by app/core/pagine_web.py where a page has the SETTINGS_FORM_JS marker, inside <script>.
   The page needs the elements: key, use-key, lang-switch, notice, tabs, panel, count, restart, restore, discard, save, and
   the i18n block. <html data-view="..."> picks the files shown as tabs (see Estensione.sorgenti); a page can define
   window.SETTINGS_HOOKS = { loaded(data), saved() } before this script. Never write the closing script tag in it. */
"use strict";
const $ = (id) => document.getElementById(id);
let I18N = { lang: "en", t: {} };
try { I18N = JSON.parse($("i18n").textContent); } catch (e) { /* page opened without the server: keys are shown */ }
const t = (key, vars = {}) => String(I18N.t[key] ?? key).replace(/\{(\w+)\}/g, (m, k) => (k in vars ? vars[k] : m));
const tx = (key, fallback) => I18N.t[key] ?? fallback;
{ const q = new URLSearchParams(location.search); q.set("lang", t("other_lang")); $("lang-switch").href = `?${q}`; }
const VIEW = document.documentElement.dataset.view || "config";  // which files this page shows (see Estensione.sorgenti)
const HOOKS = window.SETTINGS_HOOKS || {};  // a page of a node can react to loading and saving: { loaded(data), saved() }
let TABS = [];
const LOCALE = I18N.lang === "it" ? "it-IT" : "en-GB";
const S = { key: "", data: null, edits: {}, errors: {}, tab: "" };
try { S.key = sessionStorage.getItem("langbrain_key") || ""; } catch (e) { /* storage unavailable */ }
$("key").value = S.key;

const el = (tag, attrs = {}, text) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (text !== undefined) node.textContent = text;
  return node;
};
function notice(text, ok = false) {
  const n = $("notice");
  n.textContent = text || "";
  n.className = ok ? "ok" : "";
  n.style.display = text ? "block" : "none";
}
async function api(path, method = "GET", body) {
  const headers = S.key ? { "X-API-Key": S.key } : {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const r = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  let json = null;
  try { json = await r.json(); } catch (e) { /* not JSON */ }
  if (r.status === 401) throw Object.assign(new Error(t("key_invalid")), { status: 401 });
  if (r.status === 403) throw Object.assign(new Error(t("role_insufficient")), { status: 403 });
  if (!r.ok) {
    const detail = json && (typeof json.detail === "string" ? json.detail : JSON.stringify(json.detail || json));
    throw Object.assign(new Error(`${r.status}: ${detail || r.statusText}`), { status: r.status, errors: json && json.errors });
  }
  return json;
}

// ---------------------------------------------------------------- values
const asText = (f, v) => (Array.isArray(v) ? v.join(", ") : v === null || v === undefined ? "" : String(v));
const original = (f) => (f.kind === "secret" ? "" : f.kind === "bool" ? Boolean(f.value) : asText(f, f.value));
const label = (f) => tx(`fld_${f.slug}`, f.key);
const help = (f) => tx(`hlp_${f.slug}`, "");
function shown(f, v) {
  if (f.kind === "bool") return v ? t("on") : t("off");
  const s = asText(f, v);
  return s === "" ? "" : s;
}
function setEdit(f, value) {
  const same = f.kind === "secret" ? value === null : value === original(f);
  if (same) delete S.edits[f.id]; else S.edits[f.id] = value;
  delete S.errors[f.id];
  refreshBar();
}
function payload() {
  const out = {};
  for (const [id, v] of Object.entries(S.edits)) {
    const f = S.data.fields.find((x) => x.id === id);
    out[id] = f.kind === "list" ? String(v).split(",").map((s) => s.trim()).filter(Boolean) : v;
  }
  return out;
}

// ---------------------------------------------------------------- drawing
function control(f) {
  const cur = f.id in S.edits ? S.edits[f.id] : original(f);
  const id = `f-${f.slug}`;
  const onChange = (get) => (ev) => { setEdit(f, get(ev.target)); ev.target.closest(".field").classList.toggle("changed", f.id in S.edits); };
  if (f.kind === "bool") {
    const box = el("input", { type: "checkbox", id });
    box.checked = Boolean(cur);
    box.addEventListener("change", onChange((n) => n.checked));
    return box;
  }
  if (f.kind === "choice") {
    const sel = el("select", { id });
    const options = f.choices.includes(String(cur)) ? f.choices : [...f.choices, String(cur)];  // a value written by hand stays visible
    for (const c of options) sel.append(el("option", { value: c }, c === "" ? t("opt_empty") : c));
    sel.value = String(cur);
    sel.addEventListener("change", onChange((n) => n.value));
    return sel;
  }
  if (f.kind === "multiline") {
    const area = el("textarea", { id, placeholder: t("empty_prompt"), spellcheck: "false" });
    area.value = cur;
    area.addEventListener("input", onChange((n) => n.value));
    return area;
  }
  if (f.kind === "secret") {
    const input = el("input", { type: "password", id, autocomplete: "new-password", placeholder: t("secret_placeholder") });
    input.value = typeof cur === "string" ? cur : "";
    input.addEventListener("input", onChange((n) => (n.value === "" ? null : n.value)));
    return input;
  }
  const numeric = f.kind === "int" || f.kind === "float";
  const input = el("input", { type: numeric ? "number" : "text", id });
  if (numeric) { input.step = f.kind === "int" ? "1" : "any"; if (f.min !== null) input.min = String(f.min); }
  if (f.kind === "list") input.placeholder = t("list_hint");
  else if (f.default !== null && f.default !== "" && f.kind !== "choice") input.placeholder = shown(f, f.default);
  else if (f.optional) input.placeholder = t("empty_default");
  input.value = cur;
  input.addEventListener("input", onChange((n) => n.value));
  return input;
}

function fieldBox(f) {
  const box = el("div", { class: `field${f.kind === "multiline" ? " wide" : ""}` });
  const head = el("label", { for: `f-${f.slug}` }, label(f));
  if (label(f) !== f.key) head.append(el("code", {}, f.key));
  if (f.restart) head.append(el("span", { class: "badge warn" }, t("restart_badge")));
  if (f.kind === "secret") {
    const cleared = f.id in S.edits && S.edits[f.id] === "";
    head.append(el("span", { class: `badge ${f.is_set && !cleared ? "good" : ""}` }, cleared ? t("secret_will_clear") : f.is_set ? t("secret_set") : t("secret_unset")));
  }
  box.append(head);
  const ctrl = control(f);
  if (f.kind === "bool") {
    const line = el("div", { class: "inline" });
    const state = el("span", { class: "muted" }, ctrl.checked ? t("on") : t("off"));
    ctrl.addEventListener("change", () => { state.textContent = ctrl.checked ? t("on") : t("off"); });
    line.append(ctrl, state);
    box.append(line);
  } else if (f.kind === "secret") {
    const line = el("div", { class: "inline" });
    const clear = el("button", { type: "button" }, t("secret_clear"));
    clear.disabled = !f.is_set;
    clear.addEventListener("click", () => { setEdit(f, ""); ctrl.value = ""; draw(); });
    line.append(ctrl, clear);
    ctrl.style.flex = "1";
    box.append(line);
  } else {
    box.append(ctrl);
  }
  const h = help(f);
  if (h) box.append(el("div", { class: "help" }, h));
  if (f.kind !== "secret" && f.default !== null && f.default !== "" && f.kind !== "multiline") {
    box.append(el("div", { class: "help" }, `${t("default_is", { value: shown(f, f.default) })}${f.in_file ? "" : ` · ${t("not_in_file")}`}`));
  }
  if (S.errors[f.id]) { box.classList.add("invalid"); box.append(el("div", { class: "err", role: "alert" }, S.errors[f.id])); }
  if (f.id in S.edits) box.classList.add("changed");
  return box;
}

function chooseTabs() {
  const seen = [];
  for (const f of S.data.fields) if (S.data.files[f.source] && S.data.files[f.source].view === VIEW && !seen.includes(f.source)) seen.push(f.source);
  TABS = seen;
  let saved = null;
  try { saved = localStorage.getItem(`config_tab_${VIEW}`); } catch (e) { /* ignore */ }
  if (!TABS.includes(S.tab)) S.tab = TABS.includes(saved) ? saved : TABS[0];
}

function draw() {
  if (!S.data) return;
  chooseTabs();
  const tabs = $("tabs");
  tabs.replaceChildren(...TABS.map((s) => {
    const b = el("button", { type: "button", role: "tab", "aria-selected": String(S.tab === s) }, tx(`tab_${s}`, s));
    const n = S.data.fields.filter((f) => f.source === s && f.id in S.edits).length;
    if (n) b.append(el("span", { class: "badge" }, String(n)));
    b.addEventListener("click", () => { S.tab = s; try { localStorage.setItem(`config_tab_${VIEW}`, s); } catch (e) { /* ignore */ } draw(); });
    return b;
  }));
  const file = S.data.files[S.tab];
  const panel = $("panel");
  const kids = [];
  const info = el("p", { class: "muted" }, t("file_in_use", { name: file.name }));
  if (!file.exists) info.append(document.createTextNode(` · ${t("file_missing", { name: file.name })}`));
  kids.push(info);
  const groups = [];
  for (const f of S.data.fields) if (f.source === S.tab && !groups.includes(f.group)) groups.push(f.group);
  for (const g of groups) {
    const card = el("section", { class: "card" });
    card.append(el("h2", {}, tx(`grp_${S.tab}_${g}`, g)));
    const grid = el("div", { class: "fields" });
    for (const f of S.data.fields.filter((x) => x.source === S.tab && x.group === g)) grid.append(fieldBox(f));
    card.append(grid);
    kids.push(card);
  }
  panel.replaceChildren(...kids);
  refreshBar();
}

function refreshBar() {
  const n = Object.keys(S.edits).length;
  $("count").textContent = n ? t("changes_n", { n }) : t("no_changes");
  $("save").disabled = !n;
  $("discard").disabled = !n;
  $("restore").disabled = !S.data || !Object.values(S.data.files).some((f) => f.backup);
}

// ---------------------------------------------------------------- loading and saving
async function load() {
  try {
    S.data = await api("/config/api/fields");
    $("restart").hidden = !S.data.can_restart;
    notice("");
    draw();
    if (HOOKS.loaded) HOOKS.loaded(S.data);
  } catch (e) {
    notice(`${t("load_failed")}: ${e.message}`);
  }
}
async function save() {
  const changes = payload();
  if (Object.keys(changes).some((id) => id.startsWith("env:API_KEY")) && !confirm(t("key_change_warn"))) return;
  $("save").disabled = true;
  try {
    const r = await api("/config/api/save", "POST", { changes });
    S.edits = {}; S.errors = {};
    await load();
    const restart = r.restart.map((id) => label(S.data.fields.find((f) => f.id === id)));
    notice([t("saved", { n: r.saved.length }), restart.length ? t("restart_needed", { list: restart.join(", ") }) : ""].filter(Boolean).join(" "), true);
    if (HOOKS.saved) HOOKS.saved();
  } catch (e) {
    S.errors = e.errors || {};
    notice(e.message);
    draw();
  }
}
async function restore() {
  if (!confirm(t("confirm_restore"))) return;
  try {
    const r = await api("/config/api/restore", "POST", {});
    S.edits = {}; S.errors = {};
    await load();
    notice(t("restored", { files: r.restored.join(", ") }), true);
  } catch (e) {
    notice(e.status === 404 ? t("nothing_to_restore") : e.message);
  }
}
// Restart (administrator only: the server answers 403 to anyone else, the button is hidden for them).
async function restart() {
  if (Object.keys(S.edits).length) { notice(t("restart_unsaved")); return; }
  if (!confirm(t("confirm_restart"))) return;
  $("restart").disabled = true;
  try {
    await api("/config/api/restart", "POST", {});
  } catch (e) {
    $("restart").disabled = false;
    notice(`${t("restart_failed")}: ${e.message}`);
    return;
  }
  notice(t("restarting"), true);
  const alive = async () => { try { return (await fetch("/", { cache: "no-store" })).ok; } catch (e) { return false; } };
  const pause = (ms) => new Promise((r) => setTimeout(r, ms));
  const begin = Date.now(), deadline = begin + 60000;
  let wentDown = false;
  while (Date.now() < deadline) {
    await pause(1000);
    const up = await alive();
    if (!up) wentDown = true;
    else if (wentDown || Date.now() - begin > 10000) { location.reload(); return; }  // after 10 s it was restarted between two checks
  }
  $("restart").disabled = false;
  notice(t("restart_timeout"));
}
$("restart").addEventListener("click", restart);
$("save").addEventListener("click", save);
$("restore").addEventListener("click", restore);
$("discard").addEventListener("click", () => { S.edits = {}; S.errors = {}; draw(); });
$("use-key").addEventListener("click", () => {
  S.key = $("key").value.trim();
  try { sessionStorage.setItem("langbrain_key", S.key); } catch (e) { /* ignore */ }
  load();
});
window.addEventListener("beforeunload", (ev) => { if (Object.keys(S.edits).length) { ev.preventDefault(); ev.returnValue = ""; } });
load();
