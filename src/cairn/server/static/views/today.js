import { api, qs } from "../lib/api.js";
import { debounce, fmt, h } from "../lib/dom.js";
import { subscribe } from "../lib/events.js";
import { register } from "../lib/keys.js";
import { markOpened, openedIds, pref, setPref } from "../lib/store.js";
import { icon } from "../components/icons.js";
import { activeFirstRun, FirstRunScreen } from "../components/firstRun.js";
import { activateRow, jobRow } from "../components/jobRow.js";
import { pixelArt } from "../components/pixelart.js";
import { STATUS_LABELS } from "../components/pill.js";
import { menu, togglePopover } from "../components/popover.js";
import * as detail from "./detail.js";
import { STAGE_LABELS } from "./detailTracking.js";
import { openNextQueue, openPosting, postingShortcuts, setStatus } from "./postingList.js";

const RELOAD_DELAY_MS = 300;

/** When Today was last shown, in epoch ms, or null on the first visit. */
function lastLook() {
  const visits = pref("lastVisit", null);
  return typeof visits?.today === "number" ? visits.today : null;
}

function markLooked() {
  const visits = pref("lastVisit", null);
  setPref("lastVisit", { ...(visits && typeof visits === "object" ? visits : {}), today: Date.now() });
}

class Today {
  constructor(root, ctx) {
    this.root = root;
    this.ctx = ctx;
    this.data = null;
    this.cap = null;
    this.mounted = true;
    this.token = 0;
    this.picks = [];
    this.selectedId = detail.shownId();
    /** the last visit the "new" card counts from, fixed for this mount */
    this.since = lastLook();
    this.openQueue = openNextQueue();
    this.opened = openedIds();
    this.cleanups = [];
    this.reloadSoon = debounce(() => this.load(), RELOAD_DELAY_MS);
  }

  mount() {
    markLooked();
    this.greeting = h("p", { class: "today-greeting" });
    this.grid = h("div", { class: "today-grid", "aria-busy": "true" });
    this.root.replaceChildren(h("section", { class: "view today-view" },
      h("header", { class: "view-head" },
        h("div", { class: "view-heading" }, h("h1", { class: "display", text: "Today" }),
          h("span", { class: "view-count", text: new Date().toLocaleDateString(undefined,
            { weekday: "long", month: "long", day: "numeric" }) })),
        this.greeting),
      h("div", { class: "view-scroll today-body" }, this.grid)));
    this.cleanups.push(
      () => { this.mounted = false; },
      () => this.reloadSoon.cancel(),
      subscribe("application_changed", () => this.reloadSoon()),
      subscribe("tracking_changed", () => this.reloadSoon()),
      subscribe("run_done", () => this.reloadSoon()),
      subscribe("reconnected", () => this.reloadSoon()),
      detail.onApplicationChange(() => this.reloadSoon()),
      this.ctx.onStatus(() => this.renderGreeting()),
      register(postingShortcuts({ group: "Today", selected: () => this.selected(),
        move: (step) => this.move(step) })),
      detail.setNavigator({
        position: (id) => {
          const index = this.picks.findIndex((job) => job.id === id);
          return index < 0 ? null : { index: index + 1, total: this.picks.length };
        },
        step: (delta) => this.move(delta, { focus: false }),
        keys: { prev: "k", next: "j" },
      }));
    this.renderGreeting();
    this.load();
    this.followFirstRun();
    return () => this.cleanups.forEach((fn) => fn());
  }

  /** A first run still going when Today opens, after a reload say, takes the whole view. */
  async followFirstRun() {
    const run = await activeFirstRun();
    if (!run || !this.mounted) return;
    const screen = new FirstRunScreen(this.ctx);
    this.cleanups.push(() => screen.stop());
    this.root.replaceChildren(h("section", { class: "view launch-view" },
      h("div", { class: "view-scroll launch-scroll" }, screen.element)));
    screen.element.querySelector("h1").focus({ preventScroll: true });
    screen.adopt(run);
  }

  renderGreeting() {
    const { hello, summary } = this.ctx.greeting();
    this.greeting.replaceChildren(h("strong", { text: hello }), summary ? ` · ${summary}` : "");
  }

  async load() {
    const token = ++this.token;
    const since = this.since == null ? null : new Date(this.since).toISOString();
    const [today, cfg] = await Promise.allSettled([api(`/api/today${qs({ since })}`, { quiet: true }),
      api("/api/settings", { quiet: true })]);
    if (!this.mounted || token !== this.token) return;
    this.grid.removeAttribute("aria-busy");
    this.cap = cfg.status === "fulfilled" ? cfg.value.monthly_call_cap ?? null : null;
    if (today.status === "rejected") {
      if (!this.data) this.renderError(today.reason);
      return;
    }
    this.data = today.value;
    this.render();
  }

  renderError(error) {
    this.grid.replaceChildren(h("div", { class: "empty-state today-error" }, pixelArt("tray"),
      h("p", { class: "empty-title display", text: "Couldn't load today" }),
      h("p", { class: "empty-hint", text: error.status === 404
        ? "This version of Cairn can't show Today. Update Cairn and try again."
        : "Cairn didn't answer. Try again, or reopen the app." }),
      h("button", { type: "button", class: "btn btn-primary", text: "Try again", onclick: () => this.load() })));
  }

  render() {
    const active = /** @type {HTMLElement} */ (document.activeElement);
    const focused = this.grid.contains(active) ? active.dataset.key : null;
    const focusedRow = this.grid.contains(active) ? active.closest(".today-list .row") : null;
    const rowAt = focusedRow ? this.picks.findIndex((job) => job.id === focusedRow.dataset.id) : -1;
    const d = this.data;
    const before = this.picks.map((job) => job.id).join();
    this.picks = d.picks || [];
    if (this.picks.map((job) => job.id).join() !== before) this.openQueue.reset();
    this.grid.replaceChildren(
      this.picksCard(this.picks),
      this.newCard(d.new_since_last_visit, d.applied_this_week, d.closed_recently),
      this.followCard(d.follow_ups || []),
      this.weekCard(d.interviews || []),
      this.skillsCard(d.skills || []),
      this.costCard(d.cost));
    if (focused) this.grid.querySelector(`[data-key="${CSS.escape(focused)}"]`)?.focus();
    else if (focusedRow) this.refocusPick(focusedRow.dataset.id, rowAt);
    detail.refreshNav();
  }

  /** After a redraw, focus the pick that had focus, or the one now in its place. */
  refocusPick(id, index) {
    if (this.picks.some((job) => job.id === id)) {
      this.rowFor(id)?.focus({ preventScroll: true });
      return;
    }
    const next = this.picks[Math.min(Math.max(index, 0), this.picks.length - 1)];
    if (next) this.select(next.id, { focus: true });
  }

  card(title, key, ...children) {
    return this.toolCard(title, key, null, ...children);
  }

  /** A card whose heading has controls at its right end. */
  toolCard(title, key, tools, ...children) {
    const heading = h("h2", { class: "section-label", id: `today-${key}-title`, text: title });
    return h("section", { class: `panel today-card today-${key}`, "aria-labelledby": `today-${key}-title` },
      tools ? h("div", { class: "today-card-head" }, heading, tools) : heading, children);
  }

  empty(art, title, hint) {
    return h("div", { class: "today-empty" }, pixelArt(art),
      h("p", { class: "today-empty-title display", text: title }),
      h("p", { class: "muted", text: hint }));
  }

  /**
   * The matches ranked by the runs that finished since the last visit, or on the
   * first visit by the latest run.
   */
  newCard(fresh, applied, closed) {
    const count = fresh?.count ?? 0;
    const first = this.since == null;
    const noun = count === 1 ? "match" : "matches";
    const facts = [applied != null && `${fmt.plural(applied, "application")} sent this week`,
      closed ? `${fmt.plural(closed, "ranked posting")} closed in the last 7 days` : null].filter(Boolean);
    const quiet = first
      ? (fresh?.run_id == null ? "No run has ranked anything yet." : "The latest run ranked no matches.")
      : `No run has ranked a match since you last looked, ${fmt.when(new Date(this.since).toISOString())}.`;
    return this.card(first ? "New since the latest run" : "New since you last looked", "new",
      count ? h("div", { class: "today-big" },
        h("span", { class: "today-number display", text: fmt.number(count) }),
        h("span", { class: "today-number-label", text: first ? `${noun} ranked in the latest run`
          : `${noun} ranked since ${fmt.when(new Date(this.since).toISOString())}` }),
        h("button", { type: "button", class: "btn btn-sm", "data-key": "new-show", text: "Show the latest run",
          onclick: () => this.ctx.navigate("latest") }))
        : this.empty("sun", first && fresh?.run_id == null ? "Nothing ranked yet" : "All caught up", quiet),
      facts.length > 0 && h("p", { class: "today-facts muted", text: facts.join(" · ") }));
  }

  picksCard(picks) {
    if (!picks.length) {
      return this.card("Today's picks", "picks", this.empty("magnifier", "No picks yet",
        "No picks right now. Picks are your best matches with no status yet. Strong matches Cairn found "
        + "today and that were posted in the last three days come first."));
    }
    this.openNextButton = h("button", { type: "button", class: "btn btn-sm", "data-key": "open-next",
      title: "Open the next pick you have not opened yet in a new tab", onclick: () => this.openNext() });
    this.renderOpenNext();
    const stop = picks.some((job) => job.id === this.selectedId) ? this.selectedId : picks[0].id;
    const list = h("div", { class: "list today-list", role: "listbox", "aria-label": "Today's picks",
      onclick: (event) => this.onPickClick(event), onfocusin: (event) => this.onPickFocus(event) },
    picks.map((job) => {
      const selected = job.id === this.selectedId;
      const row = jobRow(job, { selected, unread: !this.opened.has(job.id) });
      row.setAttribute("aria-selected", String(selected));
      activateRow(row, job.id === stop);
      return row;
    }));
    return this.toolCard("Today's picks", "picks", this.openNextButton, list);
  }

  renderOpenNext() {
    const left = this.openQueue.candidates(this.picks, this.opened).length;
    this.openNextButton.disabled = !left;
    this.openNextButton.replaceChildren(icon("external"), left ? `Open next (${left} left)` : "All opened");
  }

  openNext() {
    const job = this.openQueue.open(this.picks, this.opened);
    if (!job) return;
    this.opened.add(job.id);
    markOpened(job.id);
    this.rowFor(job.id)?.querySelector(".row-dot")?.classList.remove("unread");
    this.renderOpenNext();
  }

  rowFor(id) {
    return [...this.grid.querySelectorAll(".today-list .row")].find((row) => row.dataset.id === id) || null;
  }

  selected() {
    return this.picks.find((job) => job.id === this.selectedId) || null;
  }

  onPickClick(event) {
    const row = event.target.closest(".row");
    const job = row && this.picks.find((known) => known.id === row.dataset.id);
    if (!job) return;
    const action = event.target.closest("[data-action]")?.dataset.action;
    if (!action) this.select(job.id, { focus: true });
    else if (action === "open") openPosting(job);
    else setStatus(job, action);
  }

  /** Tabbing onto the list's tab stop selects that pick, as j and k would. */
  onPickFocus(event) {
    const row = event.target.closest(".row");
    if (row && event.target === row && row.dataset.id !== this.selectedId) this.select(row.dataset.id);
  }

  move(step, { focus = true } = {}) {
    if (!this.picks.length) return false;
    const index = this.picks.findIndex((job) => job.id === this.selectedId);
    const from = index < 0 ? (step > 0 ? -1 : this.picks.length) : index;
    this.select(this.picks[Math.max(0, Math.min(this.picks.length - 1, from + step))].id, { focus });
    return true;
  }

  select(id, { focus = false } = {}) {
    this.selectedId = id;
    for (const row of this.grid.querySelectorAll(".today-list .row")) {
      const on = row.dataset.id === id;
      row.setAttribute("aria-current", String(on));
      row.setAttribute("aria-selected", String(on));
      activateRow(row, on);
      if (!on) continue;
      row.querySelector(".row-dot")?.classList.remove("unread");
      row.scrollIntoView({ block: "nearest" });
      if (focus) row.focus({ preventScroll: true });
    }
    this.opened.add(id);
    if (this.openNextButton?.isConnected) this.renderOpenNext();
    detail.show(id);
  }

  followCard(rows) {
    if (!rows.length) {
      return this.card("Needs a follow-up", "follow", this.empty("checklist", "Nothing to chase",
        "No application has waited two weeks without a reply."));
    }
    return this.card("Needs a follow-up", "follow", h("ul", { class: "today-rows" }, rows.map((row) => {
      const more = h("button", { type: "button", class: "icon-btn", "data-key": `follow-more-${row.id}`,
        "aria-haspopup": "menu", "aria-expanded": "false", title: "Close the application",
        "aria-label": `Close the ${row.company} application` }, icon("more"));
      more.addEventListener("click", () => togglePopover(more, () => menu([
        { label: "Mark rejected", tone: "weak", run: () => this.close(row, "rejected") },
        { label: "Mark withdrawn", run: () => this.close(row, "withdrawn") },
      ]), { label: "Close the application", align: "end" }));
      return h("li", { class: "today-row" },
        h("button", { type: "button", class: "today-row-main", "data-key": `follow-${row.id}`,
          onclick: () => this.showOther(row.id) },
        h("span", { class: "today-row-company", text: row.company || "–" }),
        h("span", { class: "today-row-title", text: row.title })),
        h("span", { class: "today-row-meta mono", title: row.applied_at ? `Applied ${fmt.full(row.applied_at)}` : null,
          text: `${STATUS_LABELS[row.status] || row.status} · ${fmt.plural(row.days_since, "day")}` }),
        h("button", { type: "button", class: "icon-btn", "data-key": `follow-note-${row.id}`, title: "Write a note",
          "aria-label": `Write a note on ${row.company}`, onclick: () => this.note(row.id) }, icon("note")),
        more);
    })));
  }

  /** Show a posting from outside the picks; j and k then start again from the top pick. */
  showOther(id) {
    this.selectedId = null;
    for (const row of this.grid.querySelectorAll(".today-list .row")) {
      row.setAttribute("aria-current", "false");
      row.setAttribute("aria-selected", "false");
    }
    return detail.show(id);
  }

  async note(id) {
    await this.showOther(id);
    detail.focusNote();
  }

  close(row, status) {
    return setStatus({ id: row.id, company: row.company, status: row.status }, status);
  }

  weekCard(interviews) {
    const calendar = h("a", { class: "btn btn-sm", href: "/api/calendar.ics", download: "cairn.ics",
      "data-key": "calendar", title: "Download every stage with a date as a calendar file" }, icon("calendar"), "Add to calendar");
    if (!interviews.length) {
      return this.card("This week", "week", this.empty("plane", "No interviews this week",
        "Add a stage with a date to an application to list it on this card."), h("div", { class: "today-card-tools" }, calendar));
    }
    return this.card("This week", "week", h("ul", { class: "today-rows" }, interviews.map((event) => h("li", { class: "today-row" },
      h("button", { type: "button", class: "today-row-main", "data-key": `stage-${event.event_id}`,
        onclick: () => this.showOther(event.id) },
      h("span", { class: "today-row-company", text: event.company || "–" }),
      h("span", { class: "today-row-title", text: STAGE_LABELS[event.stage] || event.stage })),
      h("time", { class: "today-row-meta mono", datetime: event.at, text: fmt.dateTime(event.at), title: fmt.full(event.at) }),
      event.note && h("span", { class: "today-row-note", text: event.note })))),
    h("div", { class: "today-card-tools" }, calendar));
  }

  skillsCard(skills) {
    const missing = skills.filter((skill) => skill.missing_in > 0).slice(0, 5);
    const link = h("a", { class: "link", href: "#insights", text: "See every skill in Insights" });
    if (!missing.length) {
      return this.card("Skills to close", "skills", this.empty("checklist", "No gaps found",
        "No summarized posting asks for a skill your profile lacks."), link);
    }
    const top = Math.max(...missing.map((skill) => skill.missing_in));
    return this.card("Skills to close", "skills", h("ol", { class: "bars" }, missing.map((skill) => h("li", { class: "bar-row" },
      h("span", { class: "bar-label", text: skill.skill }),
      h("span", { class: "bar-track", "aria-hidden": "true" },
        h("span", { class: "bar-fill bar-weak", style: `width: ${Math.max(4, (skill.missing_in / top) * 100)}%` })),
      h("span", { class: "bar-value mono", text: `${fmt.number(skill.missing_in)} missing`,
        title: `Missing in ${skill.missing_in} of your best matches, met in ${skill.met_in}` })))), link);
  }

  costCard(cost) {
    const total = cost?.total ?? 0;
    const counted = this.cap ? `${fmt.number(total)} of ${fmt.number(this.cap)} AI requests in the last 30 days`
      : `${fmt.plural(total, "AI request")} in the last 30 days`;
    const parts = cost && [["rank", "ranking"], ["summary", "summaries"], ["onboard", "setup"]]
      .filter(([key]) => cost[key]).map(([key, label]) => `${fmt.number(cost[key])} ${label}`).join(", ");
    const near = this.cap && total / this.cap >= 0.9;
    return h("p", { class: `today-footer muted${near ? " is-near" : ""}`, "data-key": "cost" },
      parts ? `${counted} · ${parts} · ` : `${counted} · `,
      h("a", { class: "link", href: "#settings?section=ranking", text: this.cap ? "Change the limit" : "Set a limit" }));
  }
}

/**
 * The Today view: what changed, today's picks, follow-ups, this week's stages,
 * skills to close and the month's model calls.
 * @param {HTMLElement} root
 * @param {object} ctx  see app.js `viewContext`
 * @returns {() => void} unmount
 */
export function mount(root, ctx) {
  return new Today(root, ctx).mount();
}
