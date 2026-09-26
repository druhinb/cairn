import { api } from "../lib/api.js";
import { fmt, h } from "../lib/dom.js";
import { subscribe } from "../lib/events.js";
import { eventLine, logConsole } from "../components/logConsole.js";
import { runPill } from "../components/pill.js";
import { stepper } from "../components/stepper.js";
import * as detail from "./detail.js";

const STEPS = ["Fetch", "Rank", "Summarize", "Done"];
const DRY_STEPS = ["Fetch", "Done"];
const PHASE_STEP = { fetch: 0, rank: 1 };
const PAST_LIMIT = 50;
const HOLDER_POLL_MS = 5000;
const FETCH_LINE = /^\[fetch\] (\d+) total, (\d+) relevant\/recent, (\d+) new/;
const SUMMARY_LINE = /^\[jd\] (\d+)\/(\d+) postings summarised/;
const STAMP = /^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d {2}/;
const PHASE_LINE = /^== (\w+) ==$/;
const RANK_PREFIX = "[rank] fit ";
const FINISHED = new Set(["ok", "dry-run"]);
const PAST_COLUMNS = [["new", "New"], ["ranked", "Ranked"], ["summarised", "Summarized"], ["unranked", "Unranked"]];

/**
 * The run this page follows: one it saw start on the event stream, or one already
 * going when Runs opened, adopted from /api/run/status with its log so far.
 */
const live = { run: null, listeners: new Set() };

function newRun(id, startedAt, dryRun) {
  return { id, startedAt, dryRun, step: 0, status: "running", counts: {}, ranked: 0, summarised: null,
    error: null, elapsed: null, lines: [] };
}

function readCounts(run, text) {
  const fetched = text.match(FETCH_LINE);
  const summarised = text.match(SUMMARY_LINE);
  if (fetched) Object.assign(run.counts, { total: +fetched[1], relevant: +fetched[2], new: +fetched[3] });
  if (summarised) run.summarised = [+summarised[1], +summarised[2]];
}

/** Rebuild a running run's step and counters from its log lines. */
function replay(run, lines) {
  Object.assign(run, { step: 0, counts: {}, ranked: 0, summarised: null });
  for (const line of lines) {
    const text = line.replace(STAMP, "");
    const phase = text.match(PHASE_LINE);
    if (phase && phase[1] in PHASE_STEP) run.step = PHASE_STEP[phase[1]];
    else if (text.startsWith(RANK_PREFIX)) {
      run.ranked += 1;
      run.step = 2;
    } else readCounts(run, text);
  }
}

function update(kind, data) {
  const run = live.run;
  if (kind === "run_start") {
    live.run = newRun(data.run_id, data.at * 1000, Boolean(data.dry_run));
  } else if (!run || run.status !== "running") {
    return;
  } else if (kind === "phase_start" && data.name in PHASE_STEP) {
    run.step = PHASE_STEP[data.name];
  } else if (kind === "info") {
    readCounts(run, data.text);
  } else if (kind === "posting_ranked") {
    run.ranked += 1;
    run.step = 2;
  } else if (kind === "run_done") {
    Object.assign(run, { status: data.status, step: STEPS.length, elapsed: data.elapsed,
      counts: { ...run.counts, ...data.counts } });
  } else if (kind === "run_failed") {
    Object.assign(run, { status: "failed", error: data.error, elapsed: (data.at * 1000 - run.startedAt) / 1000 });
  }
  const line = eventLine(kind, data);
  if (line && live.run) live.run.lines.push(line);
  for (const fn of live.listeners) fn(kind, line);
}

for (const kind of ["run_start", "phase_start", "phase_end", "info", "warn", "error", "posting_ranked",
  "rank_progress", "summary_progress", "run_done", "run_failed"]) {
  subscribe(kind, (data) => update(kind, data));
}

function seconds(run) {
  return run.finished_at ? (new Date(run.finished_at) - new Date(run.started_at)) / 1000 : null;
}

class Runs {
  constructor(root, ctx) {
    this.root = root;
    this.ctx = ctx;
    this.holder = { running: false, pid: null, run: null };
    this.fitThreshold = null;
    this.logView = null;
    this.logRunId = null;
    this.clock = null;
    this.poll = null;
    this.starting = false;
    this.mounted = true;
    this.cleanups = [];
  }

  mount() {
    this.build();
    const listener = (kind, line) => this.onEvent(kind, line);
    live.listeners.add(listener);
    this.cleanups.push(
      () => { this.mounted = false; },
      () => live.listeners.delete(listener),
      subscribe("reconnected", () => this.refresh()),
      () => clearInterval(this.clock),
      () => clearInterval(this.poll),
      () => detail.shownRun() != null && detail.clear(),
      () => detail.setEmptyKind("posting"),
    );
    detail.setEmptyKind("run");
    this.renderLive();
    this.refresh();
    return () => this.cleanups.forEach((fn) => fn());
  }

  build() {
    this.schedule = h("p", { class: "runs-schedule" });
    this.card = h("section", { class: "panel run-now", "aria-labelledby": "run-now-title" });
    this.livePanel = h("section", { class: "panel run-live", "aria-label": "Current run", hidden: true });
    this.past = h("section", { class: "run-past", "aria-labelledby": "past-runs-title" });
    this.root.replaceChildren(h("section", { class: "view runs-view" },
      h("header", { class: "view-head" },
        h("div", { class: "view-heading" }, h("h1", { class: "display", text: "Runs" })),
        this.schedule),
      h("div", { class: "view-scroll runs-body" }, this.card, this.livePanel, this.past)));
    this.buildCard();
    this.renderCard();
  }

  async refresh() {
    // a quiet read that fails leaves its part of the page as it was; the runs table reports its own failure
    const [holder, runs, schedule, cfg] = await Promise.all([
      api("/api/run/status", { quiet: true }).catch(() => null),
      api(`/api/runs?limit=${PAST_LIMIT}`).catch(() => null),
      api("/api/schedule", { quiet: true }).catch(() => null),
      this.fitThreshold == null ? api("/api/settings", { quiet: true }).catch(() => null) : null]);
    if (!this.mounted) return;
    if (holder) {
      this.holder = holder;
      this.ctx.syncRun(holder);
      if (holder.run && live.run?.id !== holder.run.id) this.adopt(holder.run);
      else if (!holder.running && live.run?.status === "running") this.settle(live.run);
    }
    if (cfg) this.fitThreshold = cfg.fit_threshold;
    this.renderSchedule(schedule);
    this.renderCard();
    if (runs) this.renderPast(runs);
    this.watchHolder();
  }

  /** Follow a run that was already going when Runs opened, starting from its log so far. */
  async adopt({ id, started_at: startedAt, options }) {
    const run = newRun(id, new Date(startedAt).getTime(), Boolean(options?.dry_run));
    live.run = run;
    this.renderLive();
    // without the log so far, the console starts from the events that arrive from now
    const log = await api(`/api/runs/${id}/log`, { quiet: true }).catch(() => null);
    if (log == null || live.run !== run) return;
    const logged = String(log).split("\n").filter(Boolean);
    const known = new Set(logged);
    run.lines = [...logged, ...run.lines.filter((line) => !known.has(line))];
    if (run.status === "running") replay(run, run.lines);
    if (!this.mounted) return;
    this.logView = null;
    this.renderLive();
  }

  /** Close a followed run whose lock was released without its closing event reaching this page. */
  async settle(run) {
    // the next refresh settles the run when this read fails
    const row = await api(`/api/runs/${run.id}`, { quiet: true }).catch(() => null);
    if (!row || row.status === "running" || run.status !== "running") return;
    Object.assign(run, { status: FINISHED.has(row.status) ? row.status : "failed", step: STEPS.length,
      counts: { ...run.counts, ...row.counts }, elapsed: seconds(row) });
    if (this.mounted) this.renderLive();
  }

  /** Poll the lock while a run holds it: a run in another process sends this page no events. */
  watchHolder() {
    clearInterval(this.poll);
    this.poll = null;
    if (!this.holder.running) return;
    this.poll = setInterval(async () => {
      // a missed poll waits for the next tick
      const holder = await api("/api/run/status", { quiet: true }).catch(() => null);
      if (this.mounted && holder && !holder.running) this.refresh();
    }, HOLDER_POLL_MS);
  }

  onEvent(kind, line) {
    if (kind === "run_start") this.renderLive();
    else if (line && this.logView) this.logView.append([line]);
    if (live.run) this.renderProgress();
    if (kind === "run_start" || kind === "run_done" || kind === "run_failed") {
      if (kind !== "run_start") this.renderLive();
      this.refresh();
    }
  }

  busy() {
    return live.run?.status === "running" || this.holder.running;
  }

  // -------------------------------------------------------------------------
  // Run now
  // -------------------------------------------------------------------------
  renderSchedule(schedule) {
    const link = h("a", { href: "#settings?section=schedule", class: "link", text: "Settings" });
    if (!schedule) {
      this.schedule.replaceChildren();
      return;
    }
    const at = schedule.hour == null ? null : `${schedule.hour}:${String(schedule.minute ?? 0).padStart(2, "0")}`;
    const text = schedule.installed && at ? `Daily at ${at} · ` : "Not scheduled · ";
    this.schedule.replaceChildren(h("span", { text }), link);
  }

  buildCard() {
    const limit = h("input", { type: "number", id: "run-limit", class: "input input-num", min: "1", placeholder: "all" });
    this.fit = h("input", { type: "number", id: "run-fit", class: "input input-num", min: "0", max: "100" });
    const dry = h("input", { type: "checkbox", id: "run-dry" });
    this.start = h("button", { type: "submit", class: "btn btn-primary", id: "run-start" });
    this.fields = h("fieldset", { class: "run-fields" },
      h("label", { class: "field-inline", for: "run-limit" }, h("span", { class: "field-inline-label", text: "Rank at most" }), limit,
        h("span", { class: "unit", text: "postings" })),
      h("label", { class: "field-inline", for: "run-fit" }, h("span", { class: "field-inline-label", text: "Minimum fit" }), this.fit,
        h("span", { class: "unit", text: "0–100" })),
      h("label", { class: "check" }, dry, "Fetch only, don't rank"),
      this.start);
    this.why = h("p", { class: "run-busy", role: "status", hidden: true });
    const form = h("form", { class: "run-form-inline" }, this.fields);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const body = { dry_run: dry.checked };
      if (limit.value) body.limit = Number.parseInt(limit.value, 10);
      if (this.fit.value) body.fit = Number.parseInt(this.fit.value, 10);
      this.starting = true;
      this.renderCard();
      try {
        await api("/api/run", { method: "POST", body });
      } catch {
        if (this.mounted) this.refresh();
      } finally {
        this.starting = false;
        if (this.mounted) this.renderCard();
      }
    });
    this.card.replaceChildren(
      h("div", { class: "panel-head" }, h("h2", { id: "run-now-title", class: "panel-title", text: "Run now" }),
        h("p", { class: "panel-hint", text: "Fetches new postings, ranks them against your profile and summarizes your best matches." })),
      form, this.why);
  }

  /** Update the Run now card in place, so what was typed into it survives. */
  renderCard() {
    const busy = this.busy();
    this.fields.disabled = busy;
    this.start.disabled = this.starting;
    this.start.textContent = busy ? "Running…" : "Start";
    this.fit.placeholder = this.fitThreshold == null ? "" : String(this.fitThreshold);
    this.why.hidden = !busy;
    this.why.replaceChildren(...(busy ? [h("span", { class: "run-dot run-running", "aria-hidden": "true" }),
      live.run?.status === "running" ? `Run ${live.run.id} is in progress. Its log is below.`
        : "A run is already going. You can start another when it ends."] : []));
  }

  // -------------------------------------------------------------------------
  // The live run
  // -------------------------------------------------------------------------
  renderLive() {
    const run = live.run;
    this.livePanel.hidden = !run;
    if (!run) return;
    this.progress = h("div", { class: "run-progress" });
    if (this.logRunId !== run.id || !this.logView) {
      this.logView = logConsole({ label: `Run ${run.id} log`, lines: run.lines });
      this.logRunId = run.id;
    }
    this.livePanel.replaceChildren(
      h("div", { class: "panel-head" }, h("h2", { class: "panel-title", text: `Run ${run.id}` }),
        run.dryRun && runPill("dry-run")),
      this.progress, this.logView.element);
    this.renderProgress();
    clearInterval(this.clock);
    if (run.status === "running") this.clock = setInterval(() => this.renderProgress(), 1000);
  }

  renderProgress() {
    const run = live.run;
    if (!run || !this.progress) return;
    const running = run.status === "running";
    if (!running) clearInterval(this.clock);
    const c = run.counts;
    const counters = [
      c.total != null && `fetched ${fmt.number(c.total)}`,
      c.new != null && `new ${fmt.number(c.new)}`,
      (run.ranked || c.ranked != null) && `ranked ${fmt.number(c.ranked ?? run.ranked)}${c.new != null ? `/${fmt.number(c.new)}` : ""}`,
      run.summarised && `summarized ${run.summarised[0]}/${run.summarised[1]}`,
    ].filter(Boolean).join(" · ");
    const elapsed = running ? (Date.now() - run.startedAt) / 1000 : run.elapsed;
    const failed = run.status === "failed";
    this.progress.replaceChildren(...[
      run.dryRun ? stepper(DRY_STEPS, running || failed ? 0 : DRY_STEPS.length, { label: "Run progress", failed })
        : stepper(STEPS, run.step, { label: "Run progress", failed }),
      h("p", { class: "run-counters mono" }, counters || "starting…",
        h("span", { class: "run-elapsed", text: ` · ${fmt.duration(elapsed)}` })),
      !running && this.summary(run)].filter(Boolean));
  }

  summary(run) {
    if (run.status === "failed") {
      return h("div", { class: "run-summary run-summary-failed" },
        h("p", { text: "The run failed. The log below says why." }));
    }
    const c = run.counts;
    if (run.status === "dry-run") {
      return h("div", { class: "run-summary" },
        h("p", { text: `Fetch-only run finished in ${fmt.duration(run.elapsed)} · ${fmt.plural(c.new ?? 0, "new posting")} found.` }),
        h("button", { type: "button", class: "btn", text: "Go to Jobs", onclick: () => this.ctx.navigate("jobs") }));
    }
    return h("div", { class: "run-summary" },
      h("p", { text: `Finished in ${fmt.duration(run.elapsed)} · ${fmt.plural(c.ranked ?? 0, "posting")} ranked, ${fmt.number(c.summarised ?? 0)} summarized.` }),
      h("button", { type: "button", class: "btn btn-primary", text: "Show the latest run", onclick: () => this.ctx.navigate("latest") }));
  }

  // -------------------------------------------------------------------------
  // Past runs
  // -------------------------------------------------------------------------
  renderPast(runs) {
    const shown = detail.shownRun();
    const title = h("h2", { id: "past-runs-title", class: "section-label past-title", text: "Past runs" });
    if (!runs.length) {
      this.past.replaceChildren(title, h("p", { class: "muted", text: "No runs yet. Start one above." }));
      return;
    }
    const focusedId = this.past.contains(document.activeElement)
      ? /** @type {HTMLElement} */ (document.activeElement).dataset.run : null;
    const rows = runs.map((run) => {
      const open = () => {
        for (const tr of this.past.querySelectorAll("tr[data-run]")) {
          if (tr.dataset.run === String(run.id)) tr.setAttribute("aria-current", "true");
          else tr.removeAttribute("aria-current");
        }
        detail.showRun(run.id);
      };
      const counts = run.counts || {};
      return h("tr", { tabindex: "0", "data-run": String(run.id), "aria-current": shown === run.id ? "true" : null,
        title: "Show this run's log", onclick: open,
        onkeydown: (event) => event.key === "Enter" && open() },
      h("td", { class: "mono cell-date", text: fmt.dateTime(run.started_at), title: fmt.full(run.started_at) }),
      h("td", {}, runPill(run.status)),
      h("td", { class: "mono num", text: fmt.duration(seconds(run)) }),
      PAST_COLUMNS.map(([key]) => h("td", { class: "mono num", text: fmt.number(counts[key]) })));
    });
    this.past.replaceChildren(title, h("div", { class: "table-wrap" }, h("table", { class: "data-table runs-table" },
      h("thead", {}, h("tr", {}, h("th", { scope: "col", text: "Started" }), h("th", { scope: "col", text: "Status" }),
        h("th", { scope: "col", class: "num", text: "Duration" }),
        PAST_COLUMNS.map(([, label]) => h("th", { scope: "col", class: "num", text: label })))),
      h("tbody", {}, rows))));
    if (focusedId) this.past.querySelector(`[data-run="${focusedId}"]`)?.focus();
  }
}

/**
 * The Runs view: start a run, watch it, and read past runs' logs.
 * @param {HTMLElement} root
 * @param {object} ctx  see app.js `viewContext`
 * @returns {() => void} unmount
 */
export function mount(root, ctx) {
  return new Runs(root, ctx).mount();
}
