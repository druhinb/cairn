import { api, qs } from "./lib/api.js";
import { daysAgo, debounce, fmt, h, isTyping, rove, safeUrl } from "./lib/dom.js";
import { connect, subscribe } from "./lib/events.js";
import { describe, install, keyLabel, register } from "./lib/keys.js";
import { pref, setPref } from "./lib/store.js";
import { closeCompanyCard, initCompanyCards } from "./components/companyCard.js";
import { activeFirstRun, revealOpen } from "./components/firstRun.js";
import { icon } from "./components/icons.js";
import { addCommands, closePalette, paletteOpen, togglePalette } from "./components/palette.js";
import { closePopover, openPopover, popoverOpen } from "./components/popover.js";
import { toast } from "./components/toast.js";
import { endTour, startTourWhenReady, tourOpen } from "./components/tour.js";
import * as applications from "./views/applications.js";
import * as detail from "./views/detail.js";
import * as insights from "./views/insights.js";
import * as jobs from "./views/jobs.js";
import * as runs from "./views/runs.js";
import * as settings from "./views/settings.js";
import { SECTIONS as SETTINGS_SECTIONS } from "./views/settingsFields.js";
import * as setup from "./views/setup.js";
import * as today from "./views/today.js";

const VIEWS = {
  today: { label: "Today", icon: "today", key: "1", mount: today.mount, detailOnSelect: true },
  jobs: { label: "Jobs", icon: "jobs", key: "2", mount: jobs.mount },
  latest: { label: "Latest run", icon: "latest", key: "3", mount: jobs.mount },
  saved: { label: "Saved", icon: "saved", key: "4", mount: jobs.mount },
  applications: { label: "Applications", icon: "applications", key: "5", mount: applications.mount,
    detailOnSelect: true },
  insights: { label: "Insights", icon: "insights", key: "6", mount: insights.mount, noDetail: true },
  runs: { label: "Runs", icon: "runs", key: "7", mount: runs.mount },
  settings: { label: "Settings", icon: "settings", key: "8", mount: settings.mount, noDetail: true },
  setup: { label: "Setup", mount: setup.mount, noDetail: true },
};
const DEFAULT_VIEW = "today";
const THEMES = [["auto", "Auto", "auto"], ["light", "Light", "sun"], ["dark", "Dark", "moon"],
  ["contrast", "Contrast", "contrast"]];
const DENSITIES = [["comfortable", "Comfortable", "rows"], ["compact", "Compact", "rowsCompact"]];
const PHASES = { fetch: "Fetching postings", rank: "Ranking" };
const DETAIL_MIN = 320;
const DETAIL_MAX = 560;
/** Until the pane is dragged it takes this share of the window: 380px at 1280, 448 at 1600, 520 from 1860 up. */
const DETAIL_SHARE = 0.28;
const DETAIL_DEFAULT_MIN = 380;
const DETAIL_DEFAULT_MAX = 520;
const IN_FLIGHT = ["applied", "interviewing", "offer"];
// dry and failed runs rank nothing, so the picks come from the newest run that finished
const RUNS_SCANNED = 50;
const STRONG_FIT = 80;
const PIN_LIMIT = 6;
const BADGED = ["jobs", "latest"];
const TOUR_VIEW = "jobs";

const $ = (id) => document.getElementById(id);
const railQuery = matchMedia("(max-width: 1100px)");
const contrastQuery = matchMedia("(prefers-contrast: more)");
const sheetQuery = matchMedia("(max-width: 880px)");
// the board needs the width, so a view that opens the pane on select uses the sheet sooner
const boardSheetQuery = matchMedia("(max-width: 1100px)");

const app = {
  status: null,
  relevant: null,
  picks: null,
  runs: [],
  strong: null,
  offline: false,
  schedule: null,
  fitThreshold: null,
  statusListeners: new Set(),
  hash: null,
  unmount: null,
  run: { active: false, phase: null, ranked: 0, failed: false },
  /** the tour is on its way to its first step */
  touring: false,
  detail: { collapsed: pref("detailCollapsed", false), sheetOpen: false, onSelect: false, picked: false },
  view: null,
};

// status counts, the latest run and the schedule
async function refreshStatus() {
  try {
    const [status, relevant, runs] = await Promise.all([api("/api/status", { quiet: true }),
      api("/api/jobs?limit=1", { quiet: true }), api(`/api/runs?limit=${RUNS_SCANNED}`, { quiet: true })]);
    app.status = status;
    app.relevant = relevant.total;
    app.runs = runs;
    app.picks = runs.find((run) => run.status === "ok") || null;
    app.offline = false;
  } catch {
    app.offline = true;
  }
  app.strong = await countStrong();
  renderNav();
  renderRunCard();
  for (const fn of app.statusListeners) fn();
}

/** @returns {Promise<number | null>} how many of the picks scored STRONG_FIT or more */
async function countStrong() {
  if (!app.picks) return null;
  // the greeting drops the strong count when it cannot be read
  const page = await api(`/api/jobs${qs({ run_id: app.picks.id, relevant_only: false,
    fit_min: STRONG_FIT, limit: 1 })}`, { quiet: true }).catch(() => null);
  return page?.total ?? null;
}

const refreshSoon = debounce(refreshStatus, 300);

async function loadSideFacts() {
  const [running, schedule, cfg] = await Promise.all([
    api("/api/run/status").catch(() => null),
    api("/api/schedule").catch(() => null),
    api("/api/settings").catch(() => null)]);
  if (running?.running && !app.run.active) app.run = { ...app.run, active: true, phase: null };
  app.schedule = schedule;
  app.fitThreshold = cfg?.fit_threshold ?? null;
  renderRunCard();
}

// sidebar
function buildNav() {
  $("nav").replaceChildren(...Object.entries(VIEWS).filter(([, view]) => view.key)
    .map(([name, view]) => [h("a", { href: `#${name}`, id: `nav-${name}`, class: "nav-item",
      "data-label": view.label, title: `${view.label} (${view.key})`, "aria-keyshortcuts": view.key },
    icon(view.icon), h("span", { class: "nav-label", text: view.label }),
    BADGED.includes(name) && h("span", { class: "nav-badge mono", id: `badge-${name}`, hidden: true }),
    h("span", { class: "nav-count mono", id: `count-${name}` })),
    name === "jobs" && h("ul", { id: "nav-pins", class: "nav-pins", "aria-label": "Pinned views" })]).flat()
    .filter(Boolean));
  renderPins();
}

// pinned views under Jobs
function pins() {
  const stored = pref("pins", []);
  return Array.isArray(stored) ? stored.filter((pin) => pin?.name && pin?.hash?.startsWith("#jobs")) : [];
}

function savePins(list) {
  setPref("pins", list);
  renderPins();
}

function removePin(pin) {
  const list = pins();
  const index = list.findIndex((known) => known.hash === pin.hash && known.name === pin.name);
  if (index < 0) return;
  savePins(list.filter((_, i) => i !== index));
  toast(`Unpinned “${pin.name}”`, { undo: () => {
    const now = pins();
    now.splice(Math.min(index, now.length), 0, pin);
    savePins(now.slice(0, PIN_LIMIT));
  } });
}

function renderPins() {
  $("nav-pins").replaceChildren(...pins().map((pin) => {
    const link = h("a", { href: pin.hash, class: "nav-pin", title: `${pin.name} (right-click to unpin)`,
      "aria-current": location.hash === pin.hash ? "true" : null,
      oncontextmenu: (event) => {
        event.preventDefault();
        removePin(pin);
      } }, h("span", { class: "nav-pin-name", text: pin.name }));
    return h("li", { class: "nav-pin-row" }, link,
      h("button", { type: "button", class: "nav-pin-remove", title: `Unpin ${pin.name}`,
        "aria-label": `Unpin ${pin.name}`, onclick: () => removePin(pin) }, icon("x")));
  }));
}

// "new since you last looked" badges on Jobs and Latest run
const runTime = (run) => Date.parse(run.finished_at || run.started_at) || 0;

function lastVisits() {
  const stored = pref("lastVisit", null);
  if (stored && typeof stored === "object") return stored;
  // the first launch has seen everything there is
  const now = Object.fromEntries(BADGED.map((name) => [name, Date.now()]));
  setPref("lastVisit", now);
  return now;
}

function markVisited(name) {
  if (!BADGED.includes(name)) return;
  setPref("lastVisit", { ...lastVisits(), [name]: Date.now() });
}

function renderBadges() {
  const visits = lastVisits();
  const fresh = (since) => app.runs.filter((run) => run.status === "ok" && runTime(run) > (since ?? Infinity))
    .reduce((sum, run) => sum + (run.relevant_ranked || 0), 0);
  const counts = {
    jobs: fresh(visits.jobs),
    latest: app.picks && runTime(app.picks) > (visits.latest ?? Infinity) ? app.picks.counts?.ranked || 0 : 0,
  };
  for (const name of BADGED) {
    const badge = $(`badge-${name}`);
    const n = name === app.view ? 0 : counts[name];
    badge.hidden = !n;
    badge.textContent = n ? `+${fmt.number(n)}` : "";
    badge.title = n ? `${fmt.plural(n, "posting")} ranked since you last looked` : "";
  }
}

function renderNav() {
  const status = app.status;
  const held = status?.applications || {};
  const counts = {
    jobs: app.relevant,
    latest: app.picks?.counts?.ranked,
    saved: held.saved,
    applications: status && IN_FLIGHT.reduce((sum, name) => sum + (held[name] || 0), 0),
  };
  for (const name of Object.keys(VIEWS)) {
    const count = $(`count-${name}`);
    if (count) count.textContent = counts[name] == null ? "" : fmt.number(counts[name]);
  }
  $("nav-latest").hidden = !app.picks;
  renderBadges();
  const version = app.status?.version;
  $("app-version").hidden = !version;
  $("app-version").textContent = version ? `v${version}` : "";
}

function markNav(name) {
  for (const link of $("nav").querySelectorAll(".nav-item")) {
    if (link.id === `nav-${name}`) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  renderPins();
}

function scheduleLine() {
  const schedule = app.schedule;
  if (!schedule) return null;
  const link = (text) => h("a", { href: "#settings?section=schedule", class: "link", text });
  if (!schedule.installed || schedule.hour == null) return [h("span", { text: "Not scheduled · " }), link("set up")];
  const at = `${schedule.hour}:${String(schedule.minute ?? 0).padStart(2, "0")}`;
  return [h("span", { text: `Daily at ${at} · ` }), link("change")];
}

function runLine() {
  const { run, status } = app;
  if (app.offline) return "Cairn stopped responding. Reopen it";
  if (run.active) {
    const phase = PHASES[run.phase] || "Running";
    return run.ranked ? `${phase} · ${fmt.plural(run.ranked, "posting")} ranked` : `${phase}…`;
  }
  const latest = status?.latest_run;
  if (!latest) return "No runs yet";
  if (latest.status === "failed") return `Last run failed · ${fmt.when(latest.started_at)}`;
  const found = latest.counts?.new;
  const kind = latest.status === "dry-run" ? "Fetch-only run" : "Last run";
  return `${kind} ${fmt.when(latest.started_at)}${found != null ? ` · ${fmt.number(found)} new` : ""}`;
}

function greeting() {
  const hour = new Date().getHours();
  if (hour < 12) return "Good morning";
  return hour < 18 ? "Good afternoon" : "Good evening";
}

function dayWord(value) {
  const days = daysAgo(value);
  if (days === 0) return "today";
  if (days === 1) return "yesterday";
  if (days != null && days < 7) return `on ${new Date(value).toLocaleDateString(undefined, { weekday: "long" })}`;
  return `on ${fmt.day(value)}`;
}

/**
 * "34 new yesterday, 6 strong" from the newest run that ranked, for an idle card;
 * null while a run goes, after a failed run, or before any run ranked.
 */
function idleSummary() {
  const { picks, status } = app;
  if (app.run.active || app.run.failed || app.offline || !picks || status?.latest_run?.status === "failed") return null;
  const strong = app.strong == null ? "" : `, ${fmt.number(app.strong)} strong`;
  return `${fmt.number(picks.counts?.new ?? 0)} new ${dayWord(picks.started_at)}${strong}`;
}

function renderRunCard() {
  const { run, status } = app;
  const failed = !run.active && (run.failed || status?.latest_run?.status === "failed");
  const tone = run.active ? "running" : failed ? "failed" : "idle";
  const button = h("button", { type: "button", id: "run-now", class: "btn btn-primary run-button",
    "aria-disabled": String(run.active), "aria-keyshortcuts": "r",
    title: run.active ? "A run is in progress. Open Runs to follow it" : "Run now (r)",
    onclick: () => openRunPopover(button) }, icon("play"),
  h("span", { class: "run-button-label", text: run.active ? "Running…" : "Run now" }));
  const summary = idleSummary();
  const started = summary ? app.picks.started_at : status?.latest_run?.started_at;
  $("run-card").replaceChildren(
    h("div", { class: "run-status", title: started ? `Run started ${fmt.full(started)}` : null },
      h("span", { class: `run-dot run-${tone}`, "aria-hidden": "true" }),
      summary ? h("span", { class: "run-line" }, h("strong", { class: "run-greeting", text: greeting() }),
        h("span", { text: summary })) : h("span", { class: "run-line", text: runLine() })),
    button,
    h("p", { class: "run-schedule" }, scheduleLine()));
}

function field(label, input, unit) {
  return h("label", { class: "pop-field" }, h("span", { class: "pop-field-label", text: label }),
    input, unit && h("span", { class: "pop-unit", text: unit }));
}

/**
 * Open the Run now popover under `anchor`, or open Runs while a run is going.
 * @param {HTMLElement} [anchor]
 */
function openRunPopover(anchor = $("run-now")) {
  if (app.run.active) {
    navigate("runs");
    return;
  }
  const limit = h("input", { type: "number", class: "input input-num", min: "1", placeholder: "all" });
  const fit = h("input", { type: "number", class: "input input-num", min: "0", max: "100",
    placeholder: app.fitThreshold == null ? "" : String(app.fitThreshold) });
  const dry = h("input", { type: "checkbox" });
  const start = h("button", { type: "submit", class: "btn btn-primary btn-sm", text: "Start" });
  const form = h("form", { class: "pop-form run-form" },
    h("p", { class: "pop-title", text: "Start a run" }),
    h("p", { class: "pop-hint", text: "Fetches new postings, ranks them and summarizes your best matches." }),
    field("Rank at most", limit, "postings"),
    field("Minimum fit", fit, "0–100"),
    h("label", { class: "check" }, dry, "Fetch only, don't rank"),
    h("div", { class: "pop-actions" },
      h("button", { type: "button", class: "btn btn-ghost btn-sm", text: "Cancel", onclick: () => closePopover() }),
      start));
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const body = { dry_run: dry.checked };
    if (limit.value) body.limit = Number.parseInt(limit.value, 10);
    if (fit.value) body.fit = Number.parseInt(fit.value, 10);
    start.disabled = true;
    try {
      await api("/api/run", { method: "POST", body });
      closePopover();
      app.run = { active: true, phase: null, ranked: 0, failed: false };
      renderRunCard();
    } catch {
      start.disabled = false;
    }
  });
  openPopover(anchor, form, { label: "Run now", className: "run-popover" });
}

/**
 * Match the run card to the run lock as /api/run/status reported it; a run in
 * another process sends no events, so its end shows only there.
 * @param {{running: boolean}} holder @returns {boolean} whether the card changed
 */
function syncRun(holder) {
  if (holder.running === app.run.active) return false;
  app.run = { ...app.run, active: holder.running, phase: null, ranked: 0 };
  renderRunCard();
  return true;
}

function watchRuns() {
  const update = (changes) => {
    app.run = { ...app.run, ...changes };
    renderRunCard();
  };
  subscribe("run_start", () => update({ active: true, phase: null, ranked: 0, failed: false }));
  subscribe("phase_start", ({ name }) => update({ active: true, phase: name }));
  subscribe("posting_ranked", () => update({ ranked: app.run.ranked + 1 }));
  subscribe("run_done", () => {
    update({ active: false, phase: null });
    refreshStatus();
  });
  subscribe("run_failed", ({ error }) => {
    update({ active: false, phase: null, failed: true });
    toast("The run failed. Open Runs to see why", { tone: "error" });
    refreshStatus();
  });
  subscribe("application_changed", refreshSoon);
  subscribe("reconnected", async () => {
    refreshStatus();
    const running = await api("/api/run/status").catch(() => null);
    if (running) syncRun(running);
  });
}

/** A radio group of segments; `choices` holds [value, label, icon, tooltip] entries. */
function buildRadios(root, choices, apply) {
  const buttons = choices.map(([value, label, name, title]) => h("button", {
    type: "button", role: "radio", class: "segment", "data-value": value, title,
    onclick: () => apply(value) }, icon(name), h("span", { class: "segment-label", text: label })));
  root.replaceChildren(...buttons);
  root.addEventListener("keydown", (event) => {
    const chosen = rove(event, buttons);
    if (chosen) apply(chosen.dataset.value);
  });
}

function checkRadio(root, value) {
  for (const button of root.children) {
    const checked = button.dataset.value === value;
    button.setAttribute("aria-checked", String(checked));
    button.tabIndex = checked ? 0 : -1;
  }
}

function buildSidebarFoot() {
  buildRadios($("theme-toggle"), THEMES.map(([value, label, name]) => [value, label, name,
    `${label} theme`]), applyTheme);
  buildRadios($("density-toggle"), DENSITIES.map(([value, label, name]) => [value, label, name,
    value === "compact" ? "Compact rows fit more postings on screen" : "Comfortable rows"]), applyDensity);
  contrastQuery.addEventListener("change", () => applyTheme(pref("theme", "auto")));
}

function applyTheme(theme) {
  const shown = theme === "auto" ? (contrastQuery.matches ? "contrast" : null) : theme;
  if (["light", "dark", "contrast"].includes(shown)) document.documentElement.dataset.theme = shown;
  else delete document.documentElement.dataset.theme;
  setPref("theme", theme);
  checkRadio($("theme-toggle"), theme);
}

function cycleTheme() {
  const names = THEMES.map(([value]) => value);
  const next = names[(names.indexOf(pref("theme", "auto")) + 1) % names.length];
  applyTheme(next);
  toast(`${THEMES.find(([value]) => value === next)[1]} theme`);
}

function applyDensity(density) {
  if (density === "compact") document.documentElement.dataset.density = "compact";
  else delete document.documentElement.dataset.density;
  setPref("density", density);
  checkRadio($("density-toggle"), density === "compact" ? "compact" : "comfortable");
}

function isRail() {
  const mode = pref("sidebar", "auto");
  return mode === "rail" || (mode === "auto" && railQuery.matches);
}

function applySidebar() {
  const rail = isRail();
  $("app").classList.toggle("sidebar-rail", rail);
  const toggle = $("sidebar-toggle");
  const label = rail ? "Expand sidebar" : "Collapse sidebar";
  toggle.setAttribute("aria-label", label);
  toggle.title = label;
  toggle.setAttribute("aria-expanded", String(!rail));
}

function initSidebar() {
  $("sidebar-toggle").append(icon("panelLeft"));
  $("sidebar-toggle").addEventListener("click", () => {
    setPref("sidebar", isRail() ? "full" : "rail");
    applySidebar();
  });
  railQuery.addEventListener("change", applySidebar);
  $("nav").addEventListener("click", () => {
    if (window.innerWidth <= 600 && !isRail()) {
      setPref("sidebar", "rail");
      applySidebar();
    }
  });
  applySidebar();
}

// detail pane collapse, resize and sheet
function isSheet() {
  return sheetQuery.matches || (app.detail.onSelect && boardSheetQuery.matches);
}

function applyDetail() {
  const sheet = isSheet();
  const { collapsed, onSelect, picked, sheetOpen } = app.detail;
  $("app").classList.toggle("sheet-mode", sheet);
  $("app").classList.toggle("detail-collapsed", !sheet && (onSelect ? !picked : collapsed));
  $("app").classList.toggle("sheet-open", sheet && sheetOpen);
  $("sheet-backdrop").hidden = !(sheet && sheetOpen);
}

function revealDetail() {
  if (isSheet()) app.detail.sheetOpen = true;
  else if (app.detail.onSelect) app.detail.picked = true;
  else if (app.detail.collapsed) {
    app.detail.collapsed = false;
    setPref("detailCollapsed", false);
  }
  applyDetail();
}

function hideDetail() {
  if (isSheet()) app.detail.sheetOpen = false;
  else if (app.detail.onSelect) app.detail.picked = false;
  else {
    app.detail.collapsed = true;
    setPref("detailCollapsed", true);
  }
  applyDetail();
  $("main").querySelector('[aria-selected="true"], [aria-current="true"]')?.focus({ preventScroll: true });
}

function setDetailWidth(width, persist) {
  const clamped = Math.round(Math.max(DETAIL_MIN, Math.min(DETAIL_MAX, width)));
  $("app").style.setProperty("--detail-w", `${clamped}px`);
  $("detail-handle").setAttribute("aria-valuenow", String(clamped));
  if (persist) setPref("detailWidth", clamped);
  return clamped;
}

function initDetailPane() {
  const handle = $("detail-handle");
  const fallback = Math.min(DETAIL_DEFAULT_MAX, Math.max(DETAIL_DEFAULT_MIN, window.innerWidth * DETAIL_SHARE));
  let width = setDetailWidth(pref("detailWidth", fallback), false);
  handle.setAttribute("aria-valuemin", String(DETAIL_MIN));
  handle.setAttribute("aria-valuemax", String(DETAIL_MAX));
  handle.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    handle.setPointerCapture(event.pointerId);
    $("app").classList.add("resizing");
    const move = (e) => { width = setDetailWidth(window.innerWidth - e.clientX, false); };
    const up = () => {
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", up);
      $("app").classList.remove("resizing");
      setDetailWidth(width, true);
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", up);
  });
  handle.addEventListener("keydown", (event) => {
    const step = { ArrowLeft: 16, ArrowRight: -16 }[event.key];
    if (!step) return;
    event.preventDefault();
    width = setDetailWidth(width + step, true);
  });
  $("sheet-backdrop").addEventListener("click", hideDetail);
  sheetQuery.addEventListener("change", applyDetail);
  boardSheetQuery.addEventListener("change", applyDetail);
  detail.init($("detail-body"), { reveal: revealDetail, hide: hideDetail, isSheet });
  applyDetail();
}

// help panel and shortcuts
/** The shortcut groups, the open view's first; its group is named after it. */
function helpGroups() {
  const groups = [...describe()];
  const label = VIEWS[app.view]?.label;
  const own = (group) => group === label || (group === "Jobs" && ["Latest run", "Saved"].includes(label));
  return [...groups.filter(([group]) => own(group)), ...groups.filter(([group]) => !own(group))];
}

function renderHelp() {
  $("help-body").replaceChildren(...helpGroups().map(([group, entries]) => h("section", {},
    h("h3", { class: "section-label", text: group }),
    h("dl", { class: "shortcut-list" }, entries.map(({ keys, label }) => [
      h("dt", {}, keys.filter((key) => !key.startsWith("Arrow"))
        .map((key) => h("kbd", { text: keyLabel(key) }))),
      h("dd", { text: label })])))),
  h("p", { class: "help-foot" }, h("button", { type: "button", class: "btn btn-sm",
    title: "Walk through the main controls in five steps", onclick: () => {
      toggleHelp(false);
      takeTour();
    } }, "Take the tour")));
}

function toggleHelp(open = $("help").hidden) {
  if (open) renderHelp();
  $("help").hidden = !open;
  if (open) $("help-close").focus();
  else $("help-button").focus({ preventScroll: true });
}

function registerGlobalShortcuts() {
  register([
    { keys: ["Escape"], label: "Close or clear", whileTyping: true, overOverlays: true, noPalette: true,
      run: (event) => {
        if (closePalette() || closePopover()) return true;
        if (!$("help").hidden) {
          toggleHelp(false);
          return true;
        }
        if (endTour()) return true;
        if (isTyping(event.target)) return false;
        if (isSheet() && app.detail.sheetOpen) {
          hideDetail();
          return true;
        }
        return false;
      } },
    { keys: ["mod+k"], label: "Open the command palette", whileTyping: true, overOverlays: true, noPalette: true,
      run: () => {
        closePopover();
        togglePalette();
        return true;
      } },
    { keys: ["?"], label: "Show shortcuts", run: () => toggleHelp() },
    { keys: ["r"], label: "Run now", run: () => openRunPopover() },
    ...Object.entries(VIEWS).filter(([, view]) => view.key).map(([name, view]) => ({
      keys: [view.key], label: `Go to ${view.label}`, group: "Views", overOverlays: true,
      run: () => navigate(name) })),
  ]);
  $("help-button").append(icon("help"));
  $("help-button").addEventListener("click", () => toggleHelp());
  $("help-close").append(icon("x"));
  $("help-close").addEventListener("click", () => toggleHelp(false));
}

// palette commands beyond the shortcuts, and the tour
async function fetchIcons() {
  try {
    await api("/api/icons", { method: "POST" });
    toast("Fetching company icons. They appear as they arrive");
  } catch {
    // api() has shown the error
  }
}

// updates, the provider test and a restore that just finished
function renderUpdateBanner(info) {
  const banner = $("update-banner");
  const shown = info?.newer && pref("updateDismissed", null) !== info.latest;
  banner.hidden = !shown;
  if (!shown) return;
  const url = safeUrl(info.url);
  banner.replaceChildren(h("span", { class: "update-text" }, `Version ${info.latest} is out`,
    url && [" · ", h("a", { class: "link", href: url, target: "_blank", rel: "noopener noreferrer", text: "See what changed" })]),
  h("button", { type: "button", class: "icon-btn", "aria-label": "Dismiss the update notice", title: "Dismiss",
    onclick: () => {
      setPref("updateDismissed", info.latest);
      banner.hidden = true;
    } }, icon("x")));
}

/**
 * Ask for a newer release and show or clear the sidebar banner. `force` skips the
 * day-long cache and brings back a dismissed banner; `announce` reports the answer
 * in a toast.
 * @returns {Promise<object | null>} the /api/update answer, or null when GitHub could not be reached
 */
async function checkUpdate(force = false, { announce = force } = {}) {
  // a failed check leaves the banner hidden; a forced one says so
  const info = await api("/api/update", { method: force ? "POST" : "GET", quiet: true }).catch(() => null);
  if (force && info?.newer) setPref("updateDismissed", null);
  if (announce) {
    toast(info?.newer ? `Version ${info.latest} is out` : info ? `You have the newest version, ${info.current}`
      : "Couldn't check for updates. Check your internet connection", { tone: info ? "info" : "error" });
  }
  renderUpdateBanner(info);
  return info;
}

async function testProvider() {
  toast("Testing the AI provider…");
  try {
    const answer = await api("/api/llm/test", { method: "POST", body: {}, quiet: true });
    toast(answer.ok ? `The AI provider works. It answered in ${fmt.duration(answer.latency_ms / 1000)}` : `The AI provider didn't answer: ${answer.error}`,
      { tone: answer.ok ? "info" : "error" });
  } catch (error) {
    toast(`Couldn't test the AI provider: ${error.message}`, { tone: "error" });
  }
}

function reportRestore() {
  const previous = pref("restored", null);
  if (!previous) return;
  setPref("restored", null);
  toast("Restored the backup");
}

/** The tour walks the Jobs list, so it opens Jobs first. */
async function takeTour() {
  app.touring = true;
  if (app.view !== TOUR_VIEW) navigate(TOUR_VIEW);
  try {
    await startTourWhenReady();
  } finally {
    app.touring = false;
  }
}

/** Start the tour if it has never run. @returns {boolean} whether it started */
function tourIfNew() {
  if (pref("toured", false) || app.touring || tourOpen()) return false;
  takeTour();
  return true;
}

/** Offer the tour on the first visit to Today or Jobs once setup is done. */
async function offerTour() {
  const due = () => !pref("toured", false) && !app.touring && !tourOpen() && !revealOpen()
    && [DEFAULT_VIEW, TOUR_VIEW].includes(app.view);
  if (!due()) return;
  // an unknown setup state waits for the next visit to offer the tour
  const status = await api("/api/onboard/status", { quiet: true }).catch(() => null);
  // a first run under way keeps Today for its run screen
  if (!status || status.needs_setup || await activeFirstRun()) return;
  if (due()) takeTour();
}

function registerCommands() {
  addCommands(() => [
    { id: "app:theme", label: "Toggle theme", group: "General", run: cycleTheme },
    { id: "app:icons", label: "Fetch icons now", group: "General", run: fetchIcons },
    { id: "app:tour", label: "Take the tour", group: "General", run: takeTour },
    // settings.saveBackup reports the failure itself
    { id: "app:backup", label: "Save a backup", group: "General", run: () => settings.saveBackup().catch(() => {}) },
    { id: "app:update", label: "Check for updates", group: "General", run: () => checkUpdate(true) },
    { id: "app:provider-test", label: "Test provider", group: "General", run: testProvider },
    ...SETTINGS_SECTIONS.map((section) => ({ id: `settings:${section.id}`, label: `Open settings › ${section.title}`,
      group: "Settings", run: () => navigate("settings", new URLSearchParams({ section: section.id })) })),
  ]);
}

// router
function parseHash() {
  const raw = location.hash.slice(1);
  const split = raw.indexOf("?");
  const name = split < 0 ? raw : raw.slice(0, split);
  return { name: VIEWS[name] ? name : DEFAULT_VIEW,
    params: new URLSearchParams(split < 0 ? "" : raw.slice(split + 1)) };
}

/** @param {string} name @param {URLSearchParams} [params] */
function navigate(name, params) {
  const query = params?.toString();
  location.hash = `#${name}${query ? `?${query}` : ""}`;
}

function viewContext(name, params) {
  return {
    view: name,
    params,
    setParams(next, { replace = false } = {}) {
      const query = next.toString();
      const hash = `#${name}${query ? `?${query}` : ""}`;
      if (hash === location.hash) return;
      history[replace ? "replaceState" : "pushState"](null, "", hash);
      app.hash = location.hash;
      renderPins();
    },
    navigate,
    tourIfNew,
    /** The run card's greeting and its "34 new yesterday, 6 strong" line, which is null while a run goes. */
    greeting: () => ({ hello: greeting(), summary: idleSummary() }),
    status: () => app.status,
    latestRun: () => app.picks,
    onStatus(fn) {
      app.statusListeners.add(fn);
      return () => app.statusListeners.delete(fn);
    },
    refreshStatus,
    openRunPopover,
    /** Check for a newer release now, updating the sidebar banner. @returns {Promise<object | null>} */
    checkUpdate: () => checkUpdate(true, { announce: false }),
    /** @returns {boolean} whether the sidebar holds as many pinned views as it takes */
    pinsFull: () => pins().length >= PIN_LIMIT,
    /** @param {string} hash @returns {boolean} whether that Jobs view is pinned */
    isPinned: (hash) => pins().some((pin) => pin.hash === hash),
    /** Pin a Jobs filter hash under Jobs in the sidebar. @param {string} label @param {string} hash */
    addPin(label, hash) {
      savePins([...pins().filter((pin) => pin.hash !== hash), { name: label, hash }].slice(-PIN_LIMIT));
    },
    /** @param {{running: boolean}} holder */
    syncRun(holder) {
      if (syncRun(holder)) refreshStatus();
    },
    /** @returns {boolean} whether a popover, the palette, the tour, the shortcuts panel or the detail sheet is open */
    overlayOpen: () => popoverOpen() || paletteOpen() || tourOpen() || !$("help").hidden
      || (isSheet() && app.detail.sheetOpen),
  };
}

function route() {
  if (location.hash === app.hash && app.unmount) return;
  const { name, params } = parseHash();
  app.hash = location.hash;
  closePopover();
  closePalette();
  endTour();
  closeCompanyCard();
  detail.flushNote();
  app.unmount?.();
  markVisited(app.view);
  markVisited(name);
  app.view = name;
  markNav(name);
  renderBadges();
  $("app").classList.toggle("no-detail", Boolean(VIEWS[name].noDetail));
  app.detail.onSelect = Boolean(VIEWS[name].detailOnSelect);
  app.detail.picked = false;
  app.detail.sheetOpen = false;
  applyDetail();
  document.title = `${VIEWS[name].label} · Cairn`;
  app.unmount = VIEWS[name].mount($("main"), viewContext(name, params));
  offerTour();
}

async function needsSetup() {
  try {
    return (await api("/api/onboard/status", { quiet: true })).needs_setup;
  } catch {
    // the run card reports an unreachable server, and Jobs is the safer first view
    return false;
  }
}

async function boot() {
  buildSidebarFoot();
  applyTheme(pref("theme", "auto"));
  applyDensity(pref("density", "comfortable"));
  buildNav();
  initSidebar();
  initDetailPane();
  install();
  registerGlobalShortcuts();
  registerCommands();
  initCompanyCards({ seeAll: (company) => navigate("jobs", new URLSearchParams({ q: company, rel: "0" })) });
  connect();
  watchRuns();
  const [setupNeeded] = await Promise.all([needsSetup(), refreshStatus()]);
  if (setupNeeded && !location.hash) history.replaceState(null, "", "#setup");
  window.addEventListener("hashchange", route);
  window.addEventListener("popstate", route);
  route();
  loadSideFacts();
  reportRestore();
  checkUpdate();
}

boot();
