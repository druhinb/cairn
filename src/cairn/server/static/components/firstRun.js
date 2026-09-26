import { api, qs } from "../lib/api.js";
import { fmt, h } from "../lib/dom.js";
import { subscribe } from "../lib/events.js";
import { advance, newProgress, replay, STEPS } from "../lib/firstRun.js";
import { addOverlay } from "../lib/keys.js";
import { avatar } from "./avatar.js";
import { icon } from "./icons.js";
import { eventLine, logConsole } from "./logConsole.js";
import { pixelScene } from "./pixelart.js";

const TICK_MS = 1000;
const HOLDER_POLL_MS = 5000;
const REVEAL_HOLD_MS = 2000;
// the reveal's fade-out in firstrun.css
const REVEAL_FADE_MS = 400;
const ROW_WAIT_MS = 5000;
const RUN_KINDS = ["run_start", "phase_start", "phase_end", "rank_progress", "posting_ranked",
  "summary_progress", "info", "warn", "error", "run_done", "run_failed"];
const MODIFIERS = new Set(["Shift", "Control", "Alt", "Meta", "CapsLock"]);

const LEAD = "This first run ranks the newest postings from the last two weeks. After today, each run ranks only what is new.";
const STEP_LABELS = { fetch: "Fetch postings", match: "Keep your matches", rank: "Rank them against your profile",
  read: "Read the best ones in full" };
const STATE_WORDS = { todo: "Not started", now: "In progress", done: "Done", failed: "Stopped" };

/**
 * The layout both first-run screens share: a pixel scene, a headline, a lead, the
 * steps with a mark and a note each, a clock line, a row of actions and a polite
 * live region for what changed.
 * @param {{scene: "reading" | "ranking", title: string, lead: string, steps: [string, string][]}} spec
 *   steps as [key, label]
 */
export function launchFrame({ scene, title, lead, steps }) {
  const heading = h("h1", { class: "display launch-title", tabindex: "-1", text: title });
  const leadLine = h("p", { class: "launch-lead", text: lead });
  const rows = new Map(steps.map(([key, label]) => {
    const state = h("span", { class: "visually-hidden" });
    const mark = h("span", { class: "launch-mark", "aria-hidden": "true" });
    const note = h("span", { class: "launch-note" });
    const row = h("li", { class: "launch-step is-todo" }, mark,
      h("span", { class: "launch-label" }, state, label), note);
    return [key, { row, mark, note, state }];
  }));
  const clock = h("p", { class: "launch-clock" });
  const actions = h("div", { class: "launch-actions" });
  const said = h("p", { class: "visually-hidden", "aria-live": "polite" });
  const element = h("div", { class: "launch" }, h("div", { class: "launch-art" }, pixelScene(scene)),
    heading, leadLine, h("ol", { class: "launch-steps" }, [...rows.values()].map(({ row }) => row)),
    clock, actions, said);
  return {
    element, heading, lead: leadLine, clock, actions,
    /** @param {string} key @param {"todo" | "now" | "done" | "failed"} state @param {string} [note] */
    step(key, state, note = "") {
      const found = rows.get(key);
      found.row.className = `launch-step is-${state}`;
      found.state.textContent = `${STATE_WORDS[state]}: `;
      found.mark.replaceChildren(state === "done" ? icon("check") : state === "failed" ? icon("x") : "");
      found.note.textContent = note;
    },
    /** Announce text through the live region. @param {string} text */
    say(text) {
      said.textContent = text;
    },
  };
}

/** @returns {Promise<{id: number, started_at: string, options: object} | null>} a first run under way, if any */
export async function activeFirstRun() {
  const holder = await api("/api/run/status", { quiet: true }).catch(() => null);
  return holder?.run?.options?.first_run ? holder.run : null;
}

function noteFor(step, progress) {
  const p = progress;
  const notes = {
    fetch: {
      now: p.sources ? `${fmt.plural(p.sources, "source")} so far, ${fmt.plural(p.fetched, "posting")}` : "Checking the job sites",
      done: p.sources ? `${fmt.plural(p.fetched, "posting")} from ${fmt.plural(p.sources, "source")}` : "",
    },
    match: {
      now: p.toRank != null ? `Checking that the newest ${fmt.number(p.toRank)} are still open` : "Applying your preferences",
      done: p.matched != null ? `${fmt.number(p.matched)} match, ${fmt.number(p.toRank)} to rank` : "",
    },
    rank: {
      now: p.rankProgress ? `Ranked ${fmt.number(p.rankProgress[0])} of ${fmt.number(p.rankProgress[1])}`
        : p.toRank != null ? `Scoring ${fmt.plural(p.toRank, "posting")} for fit and company tier` : "Scoring fit and company tier",
      done: `${fmt.number(p.ranked)} ranked, ${fmt.number(p.strong)} strong`,
      failed: p.unscored ? `0 of ${fmt.number(p.unscored)} scored` : null,
    },
    read: {
      now: p.summaryProgress ? `Summarized ${fmt.number(p.summaryProgress[0])} of ${fmt.number(p.summaryProgress[1])}`
        : "Summarizing what each one asks for",
      done: p.summarised ? `${fmt.number(p.summarised[0])} summarized` : p.strong ? "" : "No strong matches to read",
    },
  };
  return notes[step];
}

/**
 * The full-panel screen that follows a first run to its end from the run's events,
 * then opens Jobs under the reveal card. A page opened mid-run adopts the run from
 * its log so far.
 */
export class FirstRunScreen {
  /** @param {object} ctx  see app.js `viewContext` */
  constructor(ctx) {
    this.ctx = ctx;
    this.run = null;
    this.progress = newProgress(null);
    this.fitThreshold = null;
    this.lines = [];
    this.console = null;
    this.stops = [];
    this.frame = launchFrame({ scene: "ranking", title: "Finding your jobs", lead: LEAD,
      steps: STEPS.map((key) => [key, STEP_LABELS[key]]) });
    this.logButton = h("button", { type: "button", class: "btn btn-ghost btn-sm", "aria-expanded": "false",
      onclick: () => this.toggleLog() }, icon("list"), h("span", { text: "Show the log" }));
    this.logBox = h("div", { class: "launch-log", hidden: true });
    this.foot = h("p", { class: "launch-foot",
      text: "You can close this window. The run keeps going, and this screen picks up where it left off." });
    this.frame.actions.append(this.logButton);
    this.frame.element.append(this.foot, this.logBox);
    this.element = this.frame.element;
    this.finished = false;
    this.render();
  }

  /** Start a first run; a refusal shows on the screen as a failure to retry. */
  async start() {
    this.reset();
    this.listen();
    try {
      const { run_id: id } = await api("/api/run", { method: "POST", body: { first_run: true }, quiet: true });
      this.run ??= { id, startedAt: Date.now() };
      this.render();
    } catch (error) {
      Object.assign(this.progress, { status: "failed", error: error.message });
      this.render();
    }
  }

  /** Follow a first run already going, from its log so far. @param {{id: number, started_at: string}} run */
  async adopt(run) {
    this.reset();
    this.run = { id: run.id, startedAt: Date.parse(run.started_at) || Date.now() };
    this.listen();
    await this.resync();
  }

  stop() {
    this.stops.forEach((fn) => fn());
    this.stops = [];
  }

  reset() {
    this.stop();
    this.run = null;
    this.progress = newProgress(this.fitThreshold);
    this.lines = [];
    this.console?.setLines([]);
    this.render();
  }

  listen() {
    this.stops.push(...RUN_KINDS.map((kind) => subscribe(kind, (data) => this.onEvent(kind, data))),
      subscribe("reconnected", () => this.resync()));
    const tick = setInterval(() => this.renderClock(), TICK_MS);
    const poll = setInterval(() => this.checkHolder(), HOLDER_POLL_MS);
    this.stops.push(() => clearInterval(tick), () => clearInterval(poll));
    if (this.fitThreshold == null) {
      // without the threshold the strong count stays at zero
      api("/api/settings", { quiet: true }).then((cfg) => {
        this.fitThreshold = cfg.fit_threshold;
        this.progress.fitThreshold ??= this.fitThreshold;
      }, () => {});
    }
  }

  onEvent(kind, data) {
    if (kind === "run_start" && !this.run && data.first_run) this.run = { id: data.run_id, startedAt: data.at * 1000 };
    if (!this.run || (data.run_id != null && data.run_id !== this.run.id)) return;
    const line = eventLine(kind, data);
    if (line) {
      this.lines.push(line);
      this.console?.append([line]);
    }
    const before = this.progress.step;
    advance(this.progress, kind, data);
    this.render(before);
  }

  /** Rebuild the progress from the run's log, and settle a run that ended unseen. */
  async resync() {
    const run = this.run;
    if (!run) return;
    const log = await api(`/api/runs/${run.id}/log`, { quiet: true }).catch(() => null);
    if (log == null || this.run !== run) return;
    this.lines = String(log).split("\n").filter(Boolean);
    this.console?.setLines(this.lines);
    this.progress = replay(newProgress(this.fitThreshold), String(log));
    this.render();
    await this.checkHolder();
  }

  /** A run in another process, or one whose closing event this page missed, ends only in its row. */
  async checkHolder() {
    const run = this.run;
    if (!run || this.progress.status !== "running") return;
    const holder = await api("/api/run/status", { quiet: true }).catch(() => null);
    if (!holder || holder.running || this.run !== run) return;
    const row = await api(`/api/runs/${run.id}`, { quiet: true }).catch(() => null);
    if (!row || this.run !== run || this.progress.status !== "running") return;
    if (row.status === "ok") advance(this.progress, "run_done", { counts: row.counts });
    else {
      advance(this.progress, "run_failed", { error: row.status === "running"
        ? "The run ended without finishing. The app may have quit while it ran." : this.progress.error });
    }
    this.render();
  }

  toggleLog() {
    const open = this.logBox.hidden;
    if (open && !this.console) {
      this.console = logConsole({ label: "First run log", lines: this.lines });
      this.logBox.append(this.console.element);
    }
    this.logBox.hidden = !open;
    this.logButton.setAttribute("aria-expanded", String(open));
    this.logButton.lastChild.textContent = open ? "Hide the log" : "Show the log";
  }

  renderClock() {
    const started = this.run?.startedAt;
    if (!started || this.progress.status !== "running") return;
    this.frame.clock.textContent = `${fmt.duration((Date.now() - started) / 1000)} so far. First runs take a few minutes.`;
  }

  render(before) {
    const p = this.progress;
    const at = STEPS.indexOf(p.step);
    for (const [i, key] of STEPS.entries()) {
      const state = p.step === "done" || i < at ? "done" : i > at ? "todo" : p.status === "failed" ? "failed" : "now";
      const note = noteFor(key, p);
      this.frame.step(key, state, state === "todo" ? "" : note[state] ?? note.now);
    }
    this.renderClock();
    if (p.status === "failed") this.renderFailure();
    else if (p.status === "done") this.finish();
    else if (before && before !== p.step && p.step !== "done") this.frame.say(STEP_LABELS[p.step]);
  }

  renderFailure() {
    const p = this.progress;
    this.stop();
    this.frame.heading.textContent = !this.run ? "The first run couldn't start"
      : p.unscored ? "No postings were ranked" : "The first run stopped";
    this.frame.lead.textContent = p.unscored
      ? `The AI provider sent back no scores for ${fmt.plural(p.unscored, "posting")}. Open the log to see why.`
      : p.error ? `It failed with this error: ${p.error}` : "It stopped before it could say why. Open the log for details.";
    this.frame.lead.classList.add("field-error");
    this.frame.clock.textContent = "";
    this.foot.hidden = true;
    const retry = h("button", { type: "button", class: "btn btn-primary" }, icon("refresh"), "Try again");
    retry.addEventListener("click", () => {
      this.frame.heading.textContent = "Finding your jobs";
      this.frame.lead.textContent = LEAD;
      this.frame.lead.classList.remove("field-error");
      this.element.classList.remove("is-failed");
      this.foot.hidden = false;
      this.frame.actions.replaceChildren(this.logButton);
      this.frame.heading.focus({ preventScroll: true });
      this.start();
    });
    this.frame.actions.replaceChildren(retry, this.logButton);
    this.frame.say(`${this.frame.heading.textContent}. ${this.frame.lead.textContent}`);
    this.element.classList.add("is-failed");
    if (this.logBox.hidden && this.run) this.toggleLog();
    retry.focus({ preventScroll: true });
  }

  finish() {
    if (this.finished) return;
    this.finished = true;
    this.stop();
    this.frame.heading.textContent = "Your jobs are ready";
    this.frame.clock.textContent = "";
    showReveal({ runId: this.run?.id, counts: this.progress.counts, strong: this.progress.strong }, this.ctx.navigate);
  }
}

// ---------------------------------------------------------------------------
// The reveal: a card over Jobs that fades to show the list
// ---------------------------------------------------------------------------
let reveal = null;

/** @returns {boolean} whether the reveal card is showing */
export function revealOpen() {
  return reveal !== null;
}

addOverlay(revealOpen);

function pickCard(job) {
  const score = job.fit != null ? h("span", { class: "reveal-score mono" },
    h("span", { text: `fit ${job.fit}` }), job.tier != null && h("span", { text: `tier ${job.tier}` })) : null;
  return h("div", { class: "reveal-pick" }, avatar(job, 48),
    h("div", { class: "reveal-pick-text" }, h("span", { class: "section-label", text: "Top pick" }),
      h("strong", { class: "reveal-pick-title", text: job.title }),
      h("span", { class: "reveal-pick-company", text: job.company })),
    score);
}

function revealText(counts, strong) {
  const ranked = counts?.ranked ?? 0;
  if (ranked) return { title: "Your jobs are ready", line: `${fmt.plural(ranked, "posting")} ranked, ${fmt.number(strong)} strong. The best are at the top.` };
  return { title: "The first run finished", line: "Nothing new matched your preferences. Change them in Settings, or wait for tomorrow's run." };
}

function waitForRow() {
  return new Promise((resolve) => {
    const end = Date.now() + ROW_WAIT_MS;
    const poll = () => {
      const row = document.querySelector("#main .jobs-view .list .row");
      if (row || Date.now() > end) resolve(row);
      else setTimeout(poll, 100);
    };
    poll();
  });
}

/** Select the list's first row where the detail pane sits beside it, or only focus it where the pane covers the list. */
async function landOnFirstRow() {
  const row = await waitForRow();
  if (!row) return;
  if (document.getElementById("app")?.classList.contains("sheet-mode")) row.focus({ preventScroll: true });
  else row.click();
}

/**
 * Open the run's postings best first under a card that says the first run is done,
 * with the top pick, then fade the card away after a moment or on a click or any key.
 * Jobs groups its rows by day, so its first row is seldom the best one.
 * @param {{runId: number | undefined, counts: Record<string, number> | null, strong: number}} result
 * @param {(name: string) => void} navigate
 */
export async function showReveal({ runId, counts, strong }, navigate) {
  const top = counts?.ranked && runId != null ? await api(`/api/jobs${qs({ run_id: runId, relevant_only: false, limit: 1 })}`,
    { quiet: true }).catch(() => null) : null;
  const text = revealText(counts, strong);
  const job = top?.rows?.[0];
  const said = h("p", { class: "visually-hidden", role: "status" });
  const card = h("div", { class: "reveal-card" }, said,
    h("div", { class: "launch-art" }, pixelScene("ranking")),
    h("h2", { class: "display reveal-title", text: text.title }),
    h("p", { class: "reveal-line", text: text.line }),
    job && job.fit != null && pickCard(job),
    h("p", { class: "reveal-hint", text: "Press any key to see the list" }));
  const overlay = h("div", { class: "reveal" }, card);
  let timer = null;
  const close = () => {
    if (reveal?.overlay !== overlay) return;
    reveal = null;
    clearTimeout(timer);
    window.removeEventListener("keydown", onKey, true);
    overlay.classList.add("is-leaving");
    setTimeout(() => overlay.remove(), REVEAL_FADE_MS);
    landOnFirstRow();
  };
  const onKey = (event) => {
    if (MODIFIERS.has(event.key)) return;
    event.preventDefault();
    event.stopPropagation();
    close();
  };
  reveal = { overlay };
  overlay.addEventListener("click", close);
  window.addEventListener("keydown", onKey, true);
  document.body.append(overlay);
  requestAnimationFrame(() => { said.textContent = `${text.title}. ${text.line}`; });
  navigate("latest");
  timer = setTimeout(close, REVEAL_HOLD_MS);
}
