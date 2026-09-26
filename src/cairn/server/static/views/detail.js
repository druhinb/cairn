import { api } from "../lib/api.js";
import { copyText } from "../lib/clipboard.js";
import { fmt, h, safeUrl } from "../lib/dom.js";
import { subscribe } from "../lib/events.js";
import { keyLabel, register } from "../lib/keys.js";
import { markOpened, pref, setPref } from "../lib/store.js";
import { avatar } from "../components/avatar.js";
import { categoryChip, sourceChip, timingChip } from "../components/chip.js";
import { seeAllAt } from "../components/companyCard.js";
import { icon } from "../components/icons.js";
import { reasonOnHover } from "../components/jobRow.js";
import { band, meter } from "../components/meter.js";
import { logConsole } from "../components/logConsole.js";
import { addCommands } from "../components/palette.js";
import { runPill, STATUS_LABELS, statusPill } from "../components/pill.js";
import { statusControl } from "../components/statusControl.js";
import { toast } from "../components/toast.js";
import { addStage, renderPrep, renderStages, tracksPrep } from "./detailTracking.js";

const NOTE_DELAY_MS = 600;
const NOTE_PROMPT = "Writing a note saves the posting.";
const KEYWORD_ROWS = [["TECH", "Tech"], ["DOMAIN", "Domain"], ["MUST", "Must have"],
  ["SIGNALS", "Signals"]];
const EMPTY_VALUES = new Set(["", "none", "not stated", "n/a"]);
const BAND_WORDS = { good: "Strong", ok: "Good", weak: "Weak" };
const RUN_LOG_MS = 2000;
const ASK_WINDOW_MS = 30 * 60 * 1000;
// a quicker return is a look at the posting, too short to have applied
const ASK_AWAY_MS = 20 * 1000;
const ASKED_LIMIT = 200;
const BULK_WORKERS = 4;
const NAMES_SHOWN = 3;
const BULK_VERBS = { saved: ["Saved", "save"], passed: ["Passed", "pass on"], applied: ["Marked", "mark"] };
const FEEDBACK_REASONS = [["location", "Location"], ["compensation", "Pay"], ["role", "Role"],
  ["company", "Company"], ["seniority", "Seniority"], ["other", "Other"]];

const EMPTY_HINTS = {
  posting: {
    title: "Select a posting",
    keys: [[["j", "k"], "move through the list"], [["Enter"], "open the posting"], [["s"], "save it for later"]],
  },
  run: {
    title: "Select a run",
    keys: [[[], "Click a row to read its log"], [["r"], "start a run"]],
  },
};

const state = {
  body: null,
  /** @type {keyof EMPTY_HINTS} */
  emptyKind: "posting",
  layout: { reveal: () => {}, hide: () => {}, isSheet: () => false },
  job: null,
  /** @type {number | null} the run whose log is on show */
  run: null,
  /** the shown running run's log console and the text in it, re-read every RUN_LOG_MS */
  runLog: null,
  runLogText: null,
  runTimer: null,
  loading: null,
  request: 0,
  summarising: new Map(),
  /** postings whose score reasons are being asked for */
  explaining: new Set(),
  listeners: new Set(),
  parts: {},
  /** @type {{id: string, text: string, status: string | null} | null} typed, not yet sent */
  pendingNote: null,
  noteTimer: null,
  /** @type {Navigator | null} */
  nav: null,
  /** @type {{id: string, at: number} | null} the posting last opened on the web, until asked about */
  opened: null,
  /** the posting whose reason picker is open after a Too high */
  reasonFor: null,
  /** that posting's feedback when the picker opened, which Undo puts back */
  reasonBefore: null,
  /** dismisses the last feedback toast */
  feedbackToast: null,
  /** a link check started from the closed banner, until links_done */
  checkingLinks: false,
  /** status and stage events last drawn, so a note save leaves an open stage form alone */
  stagesDrawn: null,
};

/**
 * The list behind the pane, for its Prev and Next buttons.
 * @typedef {object} Navigator
 * @property {(id: string) => {index: number, total: number} | null} position  1-based
 * @property {(delta: number) => void} step  select the posting `delta` places away
 * @property {{prev: string, next: string}} [keys]  the view's shortcuts for step(-1) and step(1)
 */

/** @param {string} id */
const jobPath = (id) => `/api/jobs/${encodeURIComponent(id)}`;

/**
 * Attach the pane. `reveal` shows it (reopening a collapsed pane or the sheet),
 * `hide` collapses it, `isSheet` says whether it is the narrow-screen sheet.
 * @param {HTMLElement} body
 * @param {{reveal: () => void, hide: () => void, isSheet: () => boolean}} layout
 */
export function init(body, layout) {
  state.body = body;
  state.layout = layout;
  subscribe("summary_done", onSummaryDone);
  subscribe("application_changed", onRemoteChange);
  subscribe("reconnected", onReconnect);
  subscribe("run_done", onRunEnd);
  subscribe("run_failed", onRunEnd);
  subscribe("feedback_changed", onRemoteFeedback);
  subscribe("scores_explained", onRemoteReasons);
  subscribe("links_done", onLinksDone);
  subscribe("tracking_changed", onTrackingChanged);
  register([
    { keys: ["g"], label: "Agree with the score", group: "Posting", needsSelection: true,
      run: (event) => rate(event.shiftKey ? "down" : "up") },
    // Caps Lock sends G without Shift, which agrees as g does
    { keys: ["G"], label: "Mark the score too high", group: "Posting", needsSelection: true,
      run: (event) => rate(event.shiftKey ? "down" : "up") },
  ]);
  addCommands(() => (state.job && state.parts.stages ? [{ id: "detail:stage", label: "Add stage", group: "Posting",
    run: () => {
      state.layout.reveal();
      addStage(state.parts.stages, state.job, reloadShown);
    } }] : []));
  window.addEventListener("pagehide", () => flushNote({ keepalive: true }));
  document.addEventListener("visibilitychange", askIfApplied);
  window.addEventListener("focus", askIfApplied);
  renderEmpty();
}

/**
 * Remember that a posting was opened on the web, so coming back to the app between
 * twenty seconds and half an hour later asks whether you applied.
 * @param {string} id
 */
export function noteOpened(id) {
  state.opened = { id, at: Date.now() };
}

function askIfApplied() {
  const opened = state.opened;
  const job = state.job;
  if (document.visibilityState !== "visible" || !opened || !job || job.id !== opened.id) return;
  state.opened = null;
  const away = Date.now() - opened.at;
  if (away < ASK_AWAY_MS || away > ASK_WINDOW_MS || job.status || pref("asked", []).includes(job.id)) return;
  renderAsk(job);
}

function renderAsk(job) {
  const ask = state.parts.ask;
  if (!ask) return;
  const done = () => {
    ask.hidden = true;
    ask.replaceChildren();
  };
  ask.replaceChildren(h("span", { class: "ask-text", text: `Did you apply to ${job.company || "this company"}?` }),
    h("span", { class: "ask-actions" }, h("button", { type: "button", class: "btn btn-sm btn-primary", text: "Applied", title: "Mark this posting applied",
      onclick: () => {
        done();
        // api() has shown the error
        if (state.job?.id === job.id) changeStatus(state.job, "applied").catch(() => {});
      } }),
    h("button", { type: "button", class: "btn btn-sm btn-ghost", text: "Not yet", title: "Stop asking about this posting",
      onclick: () => {
        done();
        setPref("asked", [...pref("asked", []).filter((id) => id !== job.id), job.id].slice(-ASKED_LIMIT));
      } })));
  ask.hidden = false;
}

/**
 * Listen for application changes made through this module, with the updated detail.
 * @param {(job: object) => void} fn @returns {() => void}
 */
export function onApplicationChange(fn) {
  state.listeners.add(fn);
  return () => state.listeners.delete(fn);
}

/**
 * Let the pane step through a list with Prev and Next. The returned function
 * removes the navigator again, unless another has replaced it.
 * @param {Navigator} nav @returns {() => void}
 */
export function setNavigator(nav) {
  state.nav = nav;
  refreshNav();
  return () => {
    if (state.nav !== nav) return;
    state.nav = null;
    refreshNav();
  };
}

/** Redraw the Prev · Next pair, after the list behind it changed. */
export function refreshNav() {
  const bar = state.parts.nav;
  if (!bar || !state.job) return;
  const focused = bar.contains(document.activeElement)
    ? /** @type {HTMLElement} */ (document.activeElement).dataset.key : null;
  const at = state.nav?.position(state.job.id);
  const keys = state.nav?.keys;
  const step = (delta, label, name, key, disabled) => h("button", { type: "button", class: "btn btn-ghost btn-sm nav-step",
    "data-key": key, disabled,
    title: keys ? `${label} (${keyLabel(delta < 0 ? keys.prev : keys.next).toUpperCase()})` : label,
    onclick: () => state.nav?.step(delta) },
  delta < 0 && icon(name), label, delta > 0 && icon(name));
  bar.replaceChildren(...[at && step(-1, "Prev", "chevronLeft", "nav-prev", at.index <= 1),
    at && h("span", { class: "nav-position mono", text: `${fmt.number(at.index)} of ${fmt.number(at.total)}` }),
    at && step(1, "Next", "chevronRight", "nav-next", at.index >= at.total),
    h("span", { class: "filter-spacer" }), closeButton()].filter(Boolean));
  if (focused) bar.querySelector(`[data-key="${focused}"]`)?.focus();
}

/** @returns {string | null} the posting id on show */
export function shownId() {
  return state.job?.id ?? state.loading;
}

/**
 * Show one posting, fetching its detail. Responses to superseded calls are dropped.
 * `reveal: false` leaves a collapsed pane or a closed sheet as it is.
 * @param {string} id @param {{reveal?: boolean}} [options]
 */
export async function show(id, { reveal = true } = {}) {
  flushNote();
  if (reveal) state.layout.reveal();
  markOpened(id);
  if (state.opened?.id !== id) state.opened = null;
  if (state.job?.id === id) return;
  const focused = state.body.contains(document.activeElement)
    ? /** @type {HTMLElement} */ (document.activeElement).dataset.key : null;
  const request = ++state.request;
  stopRunWatch();
  state.run = null;
  state.loading = id;
  const slow = setTimeout(() => request === state.request && renderLoading(), 150);
  try {
    const job = await api(jobPath(id), { quiet: true });
    if (request !== state.request) return;
    state.job = job;
    render();
    if (focused) state.body.querySelector(`[data-key="${focused}"]`)?.focus();
  } catch (error) {
    if (request !== state.request) return;
    state.job = null;
    renderMessage(error.status === 404 ? "This posting is no longer in Cairn."
      : `Couldn't load the posting: ${error.message}`);
  } finally {
    clearTimeout(slow);
    if (request === state.request) state.loading = null;
  }
}

/** Swap the shown posting's monogram for its icon once a fetch stores one. */
export function refreshAvatar(id, domain) {
  if (state.job?.id !== id || state.job.logo_domain === domain) return;
  state.job.logo_domain = domain;
  state.body.querySelector(".detail-head .avatar")?.replaceWith(avatar(state.job, 48));
}

/** Forget the shown posting and show the empty hint. */
export function clear() {
  flushNote();
  state.request += 1;
  state.opened = null;
  stopRunWatch();
  state.job = null;
  state.run = null;
  state.loading = null;
  renderEmpty();
}

/** @returns {number | null} the run whose log is on show */
export function shownRun() {
  return state.run;
}

/**
 * Show one run's summary and its log in place of a posting, fetched afresh on
 * every call. A running run's log is re-read every two seconds until it ends.
 * @param {number} runId
 */
export function showRun(runId) {
  flushNote();
  state.opened = null;
  state.layout.reveal();
  return loadRun(runId);
}

async function loadRun(runId) {
  const again = state.run === runId;
  const request = ++state.request;
  stopRunWatch();
  state.job = null;
  state.loading = null;
  state.run = runId;
  if (!again) renderLoading();
  try {
    const [run, log] = await Promise.all([api(`/api/runs/${runId}`, { quiet: true }),
      api(`/api/runs/${runId}/log`, { quiet: true })]);
    if (request !== state.request) return;
    renderRun(run, String(log));
    if (run.status === "running") watchRun(runId, request);
  } catch (error) {
    if (request !== state.request) return;
    renderMessage(error.status === 404 ? "This run is no longer in Cairn." : `Couldn't load the run: ${error.message}`);
  }
}

function watchRun(runId, request) {
  state.runTimer = setInterval(async () => {
    // a missed read leaves the log as it was until the next tick
    const log = await api(`/api/runs/${runId}/log`, { quiet: true }).catch(() => null);
    if (log == null || request !== state.request || String(log) === state.runLogText) return;
    state.runLogText = String(log);
    state.runLog?.setLines(state.runLogText.split("\n"));
  }, RUN_LOG_MS);
}

function stopRunWatch() {
  clearInterval(state.runTimer);
  state.runTimer = null;
}

function onRunEnd({ run_id: runId }) {
  if (state.run === runId && state.runTimer) loadRun(runId);
}

const RUN_COUNTS = [["total", "Fetched"], ["relevant", "Relevant"], ["new", "New"], ["ranked", "Ranked"],
  ["unranked", "Unranked"], ["summarised", "Summarized"]];

function renderRun(run, log) {
  state.parts = {};
  const seconds = run.finished_at ? (new Date(run.finished_at) - new Date(run.started_at)) / 1000 : null;
  const counts = run.counts || {};
  const facts = RUN_COUNTS.filter(([key]) => counts[key] != null)
    .map(([key, label]) => [h("dt", { text: label }), h("dd", { class: "mono", text: fmt.number(counts[key]) })]);
  const logView = logConsole({ label: `Run ${run.id} log`, lines: log.split("\n"),
    empty: "This run wrote no log lines." });
  state.runLog = logView;
  state.runLogText = log;
  state.body.replaceChildren(h("article", { class: "detail-posting detail-run" },
    h("header", { class: "detail-head" },
      h("div", { class: "detail-head-text" },
        h("h2", { class: "detail-run-title", text: `Run ${run.id}` }),
        h("p", { class: "detail-meta" }, runPill(run.status),
          h("span", { class: "mono", text: fmt.dateTime(run.started_at), title: fmt.full(run.started_at) }),
          seconds != null && h("span", { text: `took ${fmt.duration(seconds)}` }))),
      closeButton()),
    facts.length > 0 && h("section", { class: "detail-card" },
      h("h3", { class: "section-label", text: "Counts" }), h("dl", { class: "facts run-facts" }, facts)),
    h("section", { class: "detail-section detail-log" },
      h("h3", { class: "section-label", text: "Log" }), logView.element)));
  state.body.scrollTop = 0;
}

/** Focus the note field of the shown posting. @returns {boolean} whether there was one */
export function focusNote() {
  const note = state.parts.note;
  if (!note || !state.job) return false;
  state.layout.reveal();
  note.focus();
  return true;
}

function publish(job) {
  if (state.pendingNote?.id === job.id) state.pendingNote.status = job.status ?? null;
  if (state.job?.id === job.id) {
    state.job = job;
    refreshApplicationParts();
  }
  for (const fn of state.listeners) fn(job);
}

/**
 * Send a status change. `bulk` sends it without a toast on failure and without
 * telling the listeners, for a caller that reports and publishes a batch at once.
 */
async function writeStatus(id, status, note, { bulk = false } = {}) {
  if (status == null && state.pendingNote?.id === id) {
    clearTimeout(state.noteTimer);
    state.pendingNote = null;
  }
  const job = status == null
    ? await api(`${jobPath(id)}/application`, { method: "DELETE", quiet: bulk })
    : await api(`${jobPath(id)}/application`, { method: "PUT", quiet: bulk,
      body: note == null ? { status } : { status, note } });
  if (!bulk) publish(job);
  return job;
}

function changeMessage(company, status) {
  if (status == null) return `Cleared the status for ${company}`;
  if (status === "saved") return `Saved ${company}`;
  if (status === "passed") return `Passed on ${company}`;
  return `${company} moved to ${STATUS_LABELS[status]}`;
}

/**
 * Write `status(job)` for every job, BULK_WORKERS at a time, then tell the
 * listeners about every success together.
 * @returns {Promise<{done: object[], failed: object[]}>} the jobs as given, split by outcome
 */
async function writeStatuses(jobs, status) {
  const updated = [];
  const done = [];
  const failed = [];
  let next = 0;
  const work = async () => {
    while (next < jobs.length) {
      const job = jobs[next];
      next += 1;
      try {
        updated.push(await writeStatus(job.id, status(job), null, { bulk: true }));
        done.push(job);
      } catch {
        failed.push(job);
      }
    }
  };
  await Promise.all(Array.from({ length: BULK_WORKERS }, work));
  updated.forEach(publish);
  return { done, failed };
}

function companies(jobs) {
  const names = [...new Set(jobs.map((job) => job.company || "Unknown company"))];
  const rest = names.length - NAMES_SHOWN;
  return names.slice(0, NAMES_SHOWN).join(", ") + (rest > 0 ? ` and ${fmt.number(rest)} more` : "");
}

/** "Passed 4 postings", or "Passed 2 of 4 postings · 2 failed: Ramp, Stripe" */
function bulkMessage(next, done, total) {
  const [verb] = BULK_VERBS[next] || ["Moved"];
  const noun = done === total ? fmt.plural(total, "posting") : `${fmt.number(done)} of ${fmt.plural(total, "posting")}`;
  if (next === "applied") return `${verb} ${noun} applied`;
  return BULK_VERBS[next] ? `${verb} ${noun}` : `${verb} ${noun} to ${STATUS_LABELS[next]}`;
}

/**
 * Give every posting in `jobs` the status `next` and report the outcome in one
 * toast, whose Undo restores the postings that changed.
 * @param {{id: string, company: string, status: string | null}[]} jobs @param {string} next
 * @returns {Promise<void>}
 */
export async function changeStatuses(jobs, next) {
  const changing = jobs.filter((job) => (job.status ?? null) !== next)
    .map((job) => ({ id: job.id, company: job.company, status: job.status ?? null }));
  if (!changing.length) return;
  const { done, failed } = await writeStatuses(changing, () => next);
  if (!done.length) {
    const [, action] = BULK_VERBS[next] || ["", "move"];
    toast(`Couldn't ${action} ${fmt.plural(failed.length, "posting")}: ${companies(failed)}`, { tone: "error" });
    return;
  }
  const text = bulkMessage(next, done.length, changing.length)
    + (failed.length ? ` · ${fmt.number(failed.length)} failed: ${companies(failed)}` : "");
  toast(text, { tone: failed.length ? "error" : "info", undo: async () => {
    const undo = await writeStatuses(done, (job) => job.status);
    if (undo.failed.length) {
      toast(`Couldn't undo ${fmt.plural(undo.failed.length, "posting")}: ${companies(undo.failed)}`, { tone: "error" });
    }
  } });
}

/**
 * Set or clear a posting's status and offer Undo for five seconds. Undo restores the
 * status; the note comes back only when clearing the status deleted it.
 * @param {{id: string, company: string, status: string | null, note?: string | null}} job
 * @param {string | null} next
 * @returns {Promise<object | null>} the updated detail, or null when nothing changed
 */
export async function changeStatus(job, next) {
  const before = { status: job.status ?? null, note: job.note ?? null };
  if (before.status === next) return null;
  const updated = await writeStatus(job.id, next);
  toast(changeMessage(job.company, next), {
    // api() has shown the error
    undo: () => writeStatus(job.id, before.status, next == null ? before.note : null).catch(() => {}),
  });
  return updated;
}

// rendering
function renderEmpty() {
  state.parts = {};
  const { title, keys } = EMPTY_HINTS[state.emptyKind];
  const hint = ([names, text]) => h("li", {}, names.map((key) => h("kbd", { text: key })), names.length ? " " : "", text);
  state.body.replaceChildren(h("div", { class: "detail-empty" },
    h("p", { class: "detail-empty-title", text: title }),
    h("ul", { class: "detail-empty-keys" }, keys.map(hint))));
}

/**
 * Which hint the empty pane shows while nothing is on it.
 * @param {keyof EMPTY_HINTS} kind
 */
export function setEmptyKind(kind) {
  state.emptyKind = kind;
  if (state.body && !state.job && state.run == null && !state.loading) renderEmpty();
}

function renderLoading() {
  state.parts = {};
  state.body.replaceChildren(h("div", { class: "detail-empty", "aria-busy": "true" },
    h("p", { class: "detail-empty-title", text: "Loading…" })));
}

function renderMessage(text) {
  state.parts = {};
  state.body.replaceChildren(h("div", { class: "detail-empty" },
    h("p", { class: "detail-empty-title", text })));
}

function render() {
  const job = state.job;
  state.parts = {
    nav: h("div", { class: "detail-nav" }),
    status: h("div", { class: "detail-status" }),
    ask: h("div", { class: "ask", role: "group", "aria-label": "Did you apply?", "aria-live": "polite", hidden: true }),
    stages: h("section", { class: "detail-section detail-stages", hidden: true }),
    prep: h("div", { class: "detail-prep", "data-job": job.id, hidden: true }),
    feedback: h("div", { class: "feedback" }),
    scores: h("div", { class: "scores" }),
    explain: h("div", { class: "no-summary", hidden: true }),
    closed: h("div", { class: "closed-banner", role: "status" }),
    history: h("section", { class: "detail-section" }),
    requirements: h("section", { class: "detail-card" }),
  };
  state.stagesDrawn = null;
  if (state.reasonFor !== job.id) state.reasonFor = null;
  state.body.replaceChildren(h("article", { class: "detail-posting" },
    header(job),
    state.parts.closed,
    h("div", { class: "detail-actions" }, openButton(job), state.parts.status),
    state.parts.ask,
    state.parts.stages,
    noteSection(job),
    state.parts.prep,
    scoresCard(job),
    state.parts.requirements,
    state.parts.history,
    detailsSection(job)));
  state.body.scrollTop = 0;
  refreshNav();
  refreshApplicationParts();
  renderRequirements();
  renderScores();
  renderFeedback();
  renderClosed();
}

function refreshApplicationParts() {
  const job = state.job;
  const { parts } = state;
  if (!parts.status) return;
  const focused = parts.status.contains(document.activeElement)
    ? /** @type {HTMLElement} */ (document.activeElement).dataset.key : null;
  const url = safeUrl(job.url);
  const extra = [url && { label: "Copy link", run: () => copyText(url, "the link") },
    job.keywords && { label: "Summarize again", run: () => summarise(job, { force: true }) }].filter(Boolean);
  parts.status.replaceChildren(statusControl(job.status,
    // api() has shown the error
    (next) => changeStatus(job, next).catch(() => {}), { extra }));
  if (focused) parts.status.querySelector(`[data-key="${focused}"]`)?.focus();
  if (job.status && parts.ask) parts.ask.hidden = true;
  if (parts.note && document.activeElement !== parts.note) parts.note.value = job.note || "";
  if (parts.noteHint && !job.status) parts.noteHint.textContent = NOTE_PROMPT;
  else if (parts.noteHint?.textContent === NOTE_PROMPT) parts.noteHint.textContent = "";
  renderHistory();
  const drawn = JSON.stringify([job.status, (job.events || []).filter((event) => event.stage)]);
  if (parts.stages && drawn !== state.stagesDrawn) {
    state.stagesDrawn = drawn;
    renderStages(parts.stages, job, { onChange: reloadShown });
  }
  if (parts.prep && parts.prep.hidden === tracksPrep(job)) renderPrep(parts.prep, job);
}

/**
 * Fetch the shown posting again after a tracking change and redraw what it touches.
 * @returns {Promise<object | null>} the posting, or null when it could not be read
 */
async function reloadShown() {
  // api() has shown the error
  const job = await refetchShown().catch(() => null);
  if (job) publish(job);
  return job;
}

function locationText(locations) {
  if (!locations?.length) return null;
  return locations.length > 1 ? `${locations[0]} +${locations.length - 1}` : locations[0];
}

function header(job) {
  const loc = locationText(job.locations);
  const company = job.company
    ? h("button", { type: "button", class: "company-link", "data-company": job.company,
      "data-logo": job.logo_domain || null, "aria-label": `${job.company}: see all its postings`,
      onclick: () => seeAllAt(job.company) }, job.company)
    : "Unknown company";
  return h("header", { class: "detail-head-wrap" }, state.parts.nav,
    h("div", { class: "detail-head" }, avatar(job, 48),
      h("div", { class: "detail-head-text" },
        h("h2", { class: "detail-company display" }, company),
        h("p", { class: "detail-title", text: job.title }),
        h("p", { class: "detail-meta" },
          loc && h("span", { text: loc, title: (job.locations || []).join("\n") }),
          loc && job.posted_at && h("span", { class: "meta-sep", "aria-hidden": "true", text: "·" }),
          job.posted_at && h("span", { text: `Posted ${fmt.date(job.posted_at)}`, title: fmt.full(job.posted_at) }),
          sourceChip(job.source), job.category && categoryChip(job.category)),
        job.also_on?.length > 0 && h("p", { class: "detail-also" },
          h("span", { class: "detail-also-label", text: "Also on" }),
          job.also_on.map(({ source, url }) => {
            const link = safeUrl(url);
            return link ? h("a", { class: "also-link", href: link, target: "_blank", rel: "noopener noreferrer",
              title: `Open the ${source} posting` }, sourceChip(source)) : sourceChip(source);
          })))));
}

/** The banner on a posting the last link check found closed, with Check again. */
function renderClosed() {
  const banner = state.parts.closed;
  if (!banner || !state.job) return;
  banner.hidden = !state.job.closed;
  if (banner.hidden) {
    banner.replaceChildren();
    return;
  }
  const hadFocus = banner.contains(document.activeElement);
  const busy = state.checkingLinks;
  const button = h("button", { type: "button", class: "btn btn-sm", disabled: busy, "aria-busy": String(busy),
    onclick: () => checkLinks() },
  busy && h("span", { class: "spinner", "aria-hidden": "true" }), busy ? "Checking…" : "Check again");
  banner.replaceChildren(icon("pass"), h("span", { text: "The last link check found this posting closed." }), button);
  if (hadFocus) button.focus();
}

/** Start a link check; the route checks every ranked posting, so the toast says so. */
async function checkLinks() {
  state.checkingLinks = true;
  renderClosed();
  try {
    await api("/api/insights/links", { method: "POST", quiet: true });
    toast("Checking whether your ranked postings are still open. This banner updates when it's done");
  } catch (error) {
    if (error.status === 409) {
      toast("A link check is already going");
      return;
    }
    state.checkingLinks = false;
    renderClosed();
    toast(`Couldn't start the link check: ${error.message}`, { tone: "error" });
  }
}

async function onLinksDone({ checked, closed }) {
  if (state.checkingLinks) {
    toast(checked == null ? "The link check stopped early. Try again later"
      : `Checked ${fmt.plural(checked, "link")} · ${fmt.number(closed)} closed`, { tone: checked == null ? "error" : "info" });
  }
  state.checkingLinks = false;
  const id = state.job?.id;
  if (!id) return;
  // a failed read leaves the banner as it was until the posting is shown again
  const job = await api(jobPath(id), { quiet: true }).catch(() => null);
  if (!job || state.job?.id !== id) return;
  state.job = { ...state.job, closed: job.closed };
  renderClosed();
}

function closeButton() {
  const button = h("button", { type: "button", class: "icon-btn", "aria-label": "Close details",
    onclick: () => state.layout.hide() }, icon("panelRight"));
  const label = () => {
    button.title = state.layout.isSheet() ? "Close details (Esc)" : "Close details";
  };
  label();
  button.addEventListener("pointerenter", label);
  button.addEventListener("focus", label);
  return button;
}

function openButton(job) {
  const url = safeUrl(job.url);
  if (!url) {
    return h("button", { type: "button", class: "btn btn-primary", disabled: true,
      title: "This posting has no web link" }, "No link");
  }
  return h("a", { class: "btn btn-primary", href: url, target: "_blank", rel: "noopener noreferrer",
    title: "Open posting (Enter)", ...opensPosting(job.id) }, "Open posting", icon("external"));
}

/** Listeners that note a left, Cmd or middle click on a link to the posting. */
function opensPosting(id) {
  return { onclick: () => noteOpened(id), onauxclick: (event) => event.button === 1 && noteOpened(id) };
}

/**
 * Send the note typed but not yet saved, if any, now. Runs before another posting
 * is shown, on blur, on a view change and when the page goes away.
 * @param {{keepalive?: boolean}} [options] @returns {Promise<void>}
 */
export async function flushNote({ keepalive = false } = {}) {
  clearTimeout(state.noteTimer);
  const pending = state.pendingNote;
  state.pendingNote = null;
  if (!pending) return;
  const { id, text, status } = pending;
  const hint = () => (state.job?.id === id ? state.parts.noteHint : null);
  if (hint()) hint().textContent = "Saving…";
  try {
    const body = status ? { note: text } : { status: "saved", note: text };
    const updated = await api(`${jobPath(id)}/application`, { method: "PUT", body, quiet: true, keepalive });
    publish(updated);
    if (hint()) hint().textContent = body.status ? "Saved ✓ · added to Saved" : "Saved ✓";
  } catch (error) {
    if (hint()) hint().textContent = "";
    toast(`Couldn't save the note: ${error.message}`, { tone: "error" });
  }
}

function noteSection(job) {
  const hint = h("span", { class: "note-hint", "aria-live": "polite" });
  const area = h("textarea", { id: "detail-note", class: "note", rows: "3",
    placeholder: "Add a note (referral, recruiter, deadline…)", "aria-label": "Note" });
  area.value = job.note || "";
  area.addEventListener("input", () => {
    hint.textContent = "";
    state.pendingNote = { id: job.id, text: area.value, status: state.job?.status ?? null };
    clearTimeout(state.noteTimer);
    state.noteTimer = setTimeout(flushNote, NOTE_DELAY_MS);
  });
  area.addEventListener("blur", () => flushNote());
  area.addEventListener("keydown", (event) => {
    if (event.key === "Escape") area.blur();
  });
  state.parts.note = area;
  state.parts.noteHint = hint;
  return h("section", { class: "detail-section" },
    h("div", { class: "section-head" }, h("label", { class: "section-label", for: "detail-note", text: "Note" }), hint),
    area);
}

function scoreColumn(label, value, belowFloor, reason) {
  if (value == null) return null;
  const tone = belowFloor ? "floor" : band(value);
  const word = belowFloor ? "Below your floor" : BAND_WORDS[band(value)];
  const column = h("div", { class: `score score-${tone}`, tabindex: "0" },
    h("span", { class: "score-number display", text: String(value) }),
    meter(value, { label, belowFloor }),
    h("span", { class: "score-word", text: word }));
  return reasonOnHover(column, `${label} ${value}`, reason);
}

function unrankedReason(job) {
  if (!job.relevant) return "Outside your preferences, so runs skip it.";
  if (job.seen) return "It was listed before your first run, and Cairn ranks only postings that show up after that.";
  return "The next run will rank it if it's still new.";
}

function scoresCard(job) {
  if (job.fit == null) {
    return h("section", { class: "detail-card" },
      h("h3", { class: "section-label", text: "Scores" }),
      h("p", { class: "unranked" }, h("strong", { text: "Not ranked yet. " }), unrankedReason(job)));
  }
  return h("section", { class: "detail-card" },
    h("h3", { class: "section-label", text: "Scores" }),
    state.parts.scores,
    state.parts.explain,
    state.parts.feedback);
}

function renderScores() {
  const job = state.job;
  const { scores, explain } = state.parts;
  if (!scores || !job || job.fit == null) return;
  scores.replaceChildren(scoreColumn("Fit", job.fit, false, job.fit_reason),
    scoreColumn("Company tier", job.tier, Boolean(job.below_floor), job.tier_reason) ?? h("span"));
  explain.hidden = Boolean(job.fit_reason && (job.tier == null || job.tier_reason));
  if (explain.hidden) return;
  const busy = state.explaining.has(job.id);
  const button = h("button", { type: "button", class: "btn", disabled: busy, "aria-busy": String(busy),
    onclick: () => explainScores(job) },
  busy && h("span", { class: "spinner", "aria-hidden": "true" }),
  busy ? "Explaining…" : "Explain these scores");
  explain.replaceChildren(h("p", { class: "muted",
    text: busy ? "Cairn is rereading the posting and your profile…" : "No reasons recorded for these scores." }), button);
}

/** Ask the server why the posting got its scores, then show the reasons here and in the list. */
async function explainScores(job) {
  state.explaining.add(job.id);
  renderScores();
  try {
    publish(await api(`${jobPath(job.id)}/reasons`, { method: "POST", quiet: true }));
  } catch (error) {
    toast(`Couldn't explain the scores: ${error.message}`, { tone: "error" });
  } finally {
    state.explaining.delete(job.id);
    if (state.job?.id === job.id) renderScores();
  }
}

// score feedback
function renderFeedback() {
  const box = state.parts.feedback;
  const job = state.job;
  if (!box || !job) return;
  if (job.fit == null) {
    box.replaceChildren();
    return;
  }
  const focused = box.contains(document.activeElement)
    ? /** @type {HTMLElement} */ (document.activeElement).dataset.key : null;
  const verdict = job.feedback?.verdict ?? null;
  const vote = (value, name, label, key) => h("button", { type: "button", class: `vote vote-${value}`,
    "data-key": `vote-${value}`, "aria-pressed": String(verdict === value), title: `${label} (${keyLabel(key)})`,
    onclick: () => {
      if (verdict === value) clearFeedback(job);
      else if (value === "down") openReasons(job);
      else sendFeedback(job, { verdict: value });
    } },
  icon(name), label);
  const reason = FEEDBACK_REASONS.find(([value]) => value === job.feedback?.reason)?.[1];
  const said = verdict === "up" ? "You agree with this score."
    : verdict === "down" ? (reason ? `Too high because of ${reason.toLowerCase()}.` : "You marked this score too high.") : null;
  box.replaceChildren(...[h("div", { class: "feedback-row", role: "group", "aria-label": "Is this score right?" },
    h("span", { class: "feedback-ask", text: "Is this score right?" }),
    vote("up", "thumbUp", "Agree", "g"), vote("down", "thumbDown", "Too high", "G")),
  said && state.reasonFor !== job.id && h("p", { class: "feedback-said" }, said,
    verdict === "down" && h("button", { type: "button", class: "link-btn", "data-key": "reason-change",
      text: reason ? "Change the reason" : "Add a reason", onclick: () => openReasons(job) })),
  state.reasonFor === job.id && reasonPicker(job)].filter(Boolean));
  if (focused) box.querySelector(`[data-key="${focused}"]`)?.focus();
}

/** Open the reason picker; nothing is sent until Save reason or Skip. */
function openReasons(job) {
  state.feedbackToast?.();
  state.feedbackToast = null;
  if (state.reasonFor !== job.id) state.reasonBefore = job.feedback ?? null;
  state.reasonFor = job.id;
  renderFeedback();
  state.parts.feedback?.querySelector(".reason-picker input:checked, .reason-picker input")?.focus();
}

/**
 * The picker a Too high opens. Save reason sends the verdict with the reason, and a
 * note with no reason picked goes as "other"; Skip sends the verdict alone, or only
 * closes when the score was already marked too high; Esc closes without sending.
 */
function reasonPicker(job) {
  const before = state.reasonBefore;
  const changing = before?.verdict === "down";
  const note = h("input", { type: "text", class: "input", placeholder: "Optional detail",
    "aria-label": "Why the score is too high", "aria-describedby": "reason-note-help", value: job.feedback?.note || "" });
  const close = () => {
    state.reasonFor = null;
    renderFeedback();
    state.parts.feedback?.querySelector('[data-key="vote-down"]')?.focus();
  };
  const send = (body) => {
    state.reasonFor = null;
    sendFeedback(job, body, before);
  };
  const form = h("form", { class: "reason-picker", onkeydown: (event) => {
    if (event.key !== "Escape") return;
    event.preventDefault();
    close();
  }, onsubmit: (event) => {
    event.preventDefault();
    const text = note.value.trim() || null;
    const picked = form.querySelector("input[name=reason]:checked")?.value || (text ? "other" : null);
    send(picked ? { verdict: "down", reason: picked, note: text } : { verdict: "down" });
  } },
  h("fieldset", { class: "reason-options" }, h("legend", { class: "pop-title", text: "What makes it too high?" }),
    FEEDBACK_REASONS.map(([value, label]) => h("label", { class: "reason-option" },
      h("input", { type: "radio", name: "reason", value, checked: job.feedback?.reason === value }),
      h("span", { text: label })))),
  note,
  h("p", { class: "field-help", id: "reason-note-help", text: "If you pick no reason, the note saves under Other." }),
  h("div", { class: "pop-actions" },
    h("button", { type: "button", class: "btn btn-ghost btn-sm", text: changing ? "Cancel" : "Skip",
      title: changing ? "Keep the reason as it is" : "Mark the score too high without a reason",
      onclick: () => (changing ? close() : send({ verdict: "down" })) }),
    h("button", { type: "submit", class: "btn btn-primary btn-sm", text: "Save reason" })));
  return form;
}

function showFeedback(job, feedback) {
  if (state.job?.id !== job.id) return;
  publish({ ...state.job, feedback: feedback && { verdict: feedback.verdict, reason: feedback.reason ?? null,
    note: feedback.note ?? null } });
  renderFeedback();
}

/**
 * PUT one verdict and offer Undo, which puts back `before`: the feedback held
 * before the key or button that led here.
 */
async function sendFeedback(job, body, before = state.job?.id === job.id ? state.job.feedback : job.feedback) {
  let saved;
  try {
    saved = await api(`${jobPath(job.id)}/feedback`, { method: "PUT", body });
  } catch {
    // api() has shown the error
    renderFeedback();
    return;
  }
  showFeedback(job, saved);
  // the closed picker held focus
  if (document.activeElement === document.body) {
    state.parts.feedback?.querySelector(`[data-key="vote-${body.verdict}"]`)?.focus({ preventScroll: true });
  }
  const undo = async () => {
    try {
      if (before) showFeedback(job, await api(`${jobPath(job.id)}/feedback`, { method: "PUT", body: before }));
      else {
        await api(`${jobPath(job.id)}/feedback`, { method: "DELETE" });
        showFeedback(job, null);
      }
    } catch {
      // api() has shown the error
    }
  };
  state.feedbackToast = toast(body.verdict === "up" ? `You agreed with the score for ${job.company || "this posting"}`
    : `You marked the score for ${job.company || "this posting"} too high`, { undo });
}

async function clearFeedback(job) {
  try {
    await api(`${jobPath(job.id)}/feedback`, { method: "DELETE" });
  } catch {
    return;
  }
  state.reasonFor = null;
  showFeedback(job, null);
}

/**
 * Agree with the shown posting's score, or mark it too high and open the reason
 * picker; for the g and G keys. Views without the pane leave the key alone.
 * @param {"up" | "down"} verdict @returns {boolean} whether a scored posting is on show
 */
function rate(verdict) {
  const job = state.job;
  if (!job || job.fit == null || document.getElementById("app")?.classList.contains("no-detail")) return false;
  state.layout.reveal();
  if (verdict === "down") openReasons(job);
  else if (job.feedback?.verdict !== "up") sendFeedback(job, { verdict });
  return true;
}

async function onRemoteFeedback({ id, verdict }) {
  if (state.job?.id !== id || (state.job.feedback?.verdict ?? null) === (verdict ?? null)) return;
  const job = await refetchShown().catch(() => null);
  if (job) renderFeedback();
}

async function onRemoteReasons({ id }) {
  if (state.job?.id !== id || state.job.fit_reason) return;
  const job = await refetchShown().catch(() => null);
  if (job) renderScores();
}

async function onTrackingChanged({ id }) {
  if (state.job?.id !== id) return;
  const job = await refetchShown().catch(() => null);
  if (!job) return;
  refreshApplicationParts();
  renderPrep(state.parts.prep, job);
}

function parseKeywords(text) {
  const lines = {};
  for (const line of String(text || "").split("\n")) {
    const match = line.match(/^\s*([A-Z]+)\s*:\s*(.*)$/);
    if (match) lines[match[1]] = match[2].trim();
  }
  return lines;
}

function splitValues(value) {
  return String(value || "").split(/\s*[,;]\s*/)
    .filter((part) => !EMPTY_VALUES.has(part.toLowerCase()));
}

function renderRequirements() {
  const job = state.job;
  const card = state.parts.requirements;
  if (!card) return;
  const title = h("h3", { class: "section-label", text: "Requirements" });
  if (!job.keywords) {
    const busy = state.summarising.has(job.id);
    const button = h("button", { type: "button", class: "btn", disabled: busy,
      "aria-busy": String(busy), onclick: () => summarise(job) },
    busy && h("span", { class: "spinner", "aria-hidden": "true" }),
    busy ? "Summarizing…" : "Summarize requirements");
    card.replaceChildren(title, h("div", { class: "no-summary" },
      h("p", { class: "muted", text: busy ? "Reading the posting. This takes 10 to 30 seconds…" : "No summary yet." }),
      button));
    return;
  }
  const lines = parseKeywords(job.keywords);
  const gap = job.gap || { met: [], missing: [] };
  const gapRow = (label, values, tone) => values.length > 0 && [h("dt", { text: label }),
    h("dd", {}, values.map((value) => h("span", { class: `chip ${tone}`, text: value })))];
  const rows = KEYWORD_ROWS.map(([key, label]) => {
    const values = splitValues(lines[key]);
    return values.length ? [h("dt", { text: label }),
      h("dd", {}, values.map((value) => h("span", { class: "chip chip-fill", text: value })))] : null;
  });
  if (job.timing) {
    rows.push([h("dt", { text: "Timing" }),
      h("dd", {}, timingChip(job.timing, job.starts_before_graduation))]);
  }
  const gapRows = [gapRow("You have", gap.met, "chip-met"), gapRow("Missing", gap.missing, "chip-missing")];
  const busy = state.summarising.has(job.id);
  card.replaceChildren(...[title,
    busy && h("p", { class: "muted", "aria-busy": "true" }, h("span", { class: "spinner", "aria-hidden": "true" }),
      " Reading the posting again (10–30 s)…"),
    gapRows.some(Boolean) && h("dl", { class: "requirements gap" }, gapRows),
    h("dl", { class: "requirements" }, rows)].filter(Boolean));
}

async function summarise(job, { force = false } = {}) {
  state.summarising.set(job.id, job.company);
  renderRequirements();
  try {
    await api(`${jobPath(job.id)}/summary${force ? "?force=1" : ""}`, { method: "POST", quiet: true });
  } catch (error) {
    state.summarising.delete(job.id);
    toast(`Couldn't start the summary: ${error.message}`, { tone: "error" });
    if (state.job?.id === job.id) renderRequirements();
  }
}

async function onSummaryDone({ id, err }) {
  const company = state.summarising.get(id);
  state.summarising.delete(id);
  if (company !== undefined && err) toast(`Couldn't summarize ${company}: ${err}`, { tone: "error" });
  if (state.job?.id !== id) return;
  try {
    const job = await api(jobPath(id));
    if (state.job?.id !== id) return;
    state.job = job;
  } catch {
    // api() has shown the error
  } finally {
    if (state.job?.id === id) renderRequirements();
  }
}

async function refetchShown() {
  const id = state.job?.id;
  if (!id) return null;
  const job = await api(jobPath(id));
  if (state.job?.id !== id) return null;
  state.job = job;
  if (state.pendingNote?.id === id) state.pendingNote.status = job.status ?? null;
  return job;
}

async function onRemoteChange({ id }) {
  if (state.job?.id !== id) return;
  // api() has shown the error
  if (await refetchShown().catch(() => null)) refreshApplicationParts();
}

async function onReconnect() {
  state.summarising.clear();
  if (!state.job) return;
  try {
    await refetchShown();
  } catch {
    // api() has shown the error
  } finally {
    if (state.job) {
      refreshApplicationParts();
      renderRequirements();
    }
  }
}

function renderHistory() {
  const events = (state.job.events || []).filter((event) => !event.stage).reverse();
  const section = state.parts.history;
  section.hidden = events.length === 0;
  section.replaceChildren(h("h3", { class: "section-label", text: "History" }),
    h("ol", { class: "timeline" }, events.map((event) => h("li", {},
      statusPill(event.status),
      h("time", { class: "mono muted", datetime: event.at, text: fmt.dateTime(event.at), title: fmt.full(event.at) }),
      event.note && h("span", { class: "timeline-note", text: event.note })))));
}

function listText(values) {
  return values?.length ? values.join(", ") : "–";
}

function detailsSection(job) {
  const url = safeUrl(job.url);
  const row = (label, value) => [h("dt", { text: label }), h("dd", {}, value)];
  const details = h("details", { class: "detail-section details", open: pref("detailsOpen", false) },
    h("summary", {}, h("span", { class: "section-label", text: "Details" })),
    h("dl", { class: "facts" },
      row("Posting id", h("button", { type: "button", class: "mono copy-id", title: "Copy",
        onclick: () => copyText(job.id, "the posting id") }, job.id, icon("copy"))),
      row("Category", job.category || "–"),
      row("Degrees", listText(job.degrees)),
      row("Terms", listText(job.terms)),
      row("Sponsorship", job.sponsorship || "–"),
      row("First seen", h("span", { class: "mono", text: fmt.dateTime(job.first_seen_at), title: fmt.full(job.first_seen_at) })),
      row("Last seen", h("span", { class: "mono", text: fmt.dateTime(job.last_seen_at), title: fmt.full(job.last_seen_at) })),
      row("Active", job.active ? "Yes"
        : h("span", { class: "tone-weak", text: "No · no longer listed" })),
      row("Source", job.source || "–"),
      row("Source URL", url ? h("a", { href: url, target: "_blank", rel: "noopener noreferrer",
        class: "link", text: new URL(url).host, ...opensPosting(job.id) }) : "–")));
  details.addEventListener("toggle", () => setPref("detailsOpen", details.open));
  return details;
}
