import { api, qs } from "../lib/api.js";
import { daysAgo, debounce, fmt, h, isTyping } from "../lib/dom.js";
import { subscribe } from "../lib/events.js";
import { register } from "../lib/keys.js";
import { markOpened, openedIds, pref, setPref } from "../lib/store.js";
import { avatar } from "../components/avatar.js";
import { describeSource } from "../components/chip.js";
import { icon } from "../components/icons.js";
import { activateRow, jobRow } from "../components/jobRow.js";
import { pixelArt } from "../components/pixelart.js";
import { STATUS_LABELS } from "../components/pill.js";
import { addCommands } from "../components/palette.js";
import { closePopover, togglePopover } from "../components/popover.js";
import { switchControl } from "../components/switch.js";
import { toast } from "../components/toast.js";
import * as detail from "./detail.js";
import { openNextQueue, openPosting, postingShortcuts, setStatus } from "./postingList.js";
import { DEFAULTS, SORTS, categoryPanel, locationPanel, narrowed, payLabel, payPanel, postedPanel,
  readFilters, sourcePanel, sponsorPanel, statusPanel, thresholdPanel, writeFilters } from "./jobsFilters.js";

const PAGE = 50;
const LIMIT_MAX = 200;
const SEARCH_DELAY_MS = 200;
const GROUPS = [["date", "By date"], ["company", "By company"], ["none", "None"]];
// /api/insights/why rule names and the preference each one reads
const WHY_RULES = {
  "exclude": ["title_exclude", "Skip titles with"],
  "field exclude": ["title_exclude_field", "Skip field roles with"],
  "intern term": ["wanted_intern_terms", "Internship terms"],
  "experience": ["max_years_required", "Most years a job can ask for"],
  "category": ["allowed_categories", "Categories"],
  "title keyword": ["title_keywords", "Title keywords"],
  "degree": ["degrees_held", "Degrees"],
  "location": ["location_allow", "Locations"],
  "recent": ["recent_days", "Recent days"],
};
const FADE_ROWS = 10;
const PIN_NAME_MAX = 24;
const selectModeQuery = matchMedia("(max-width: 899px)");

const SCOPES = {
  jobs: { title: "Jobs", fixed: {}, hidden: new Set(), group: ["group", "date"] },
  saved: { title: "Saved", fixed: { status: ["saved"], rel: false },
    hidden: new Set(["rel", "status", "passed"]), group: ["group", "date"] },
  latest: { title: "Latest run", fixed: { rel: false }, hidden: new Set(["rel"]),
    group: ["latestGroup", "none"], byRun: true },
};

function dateBucket(posted) {
  const days = daysAgo(posted);
  if (days == null) return "Undated";
  if (days <= 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days < 7) return "This week";
  return "Earlier";
}

const BUCKET_ORDER = ["Today", "Yesterday", "This week", "Earlier", "Undated"];
const GROUP_LABELS = {
  none: () => null,
  date: (row) => dateBucket(row.posted_at),
  company: (row) => row.company || "–",
};

class JobsList {
  constructor(root, ctx, scope) {
    this.root = root;
    this.ctx = ctx;
    this.scope = SCOPES[scope];
    this.filters = readFilters(ctx.params);
    this.group = pref(...this.scope.group);
    this.runId = ctx.latestRun()?.id ?? null;
    this.rows = [];
    this.rowEls = new Map();
    this.groups = [];
    this.groupsByLabel = new Map();
    this.order = [];
    this.removed = new Map();
    this.chips = new Map();
    this.total = 0;
    this.loading = false;
    this.token = 0;
    this.selectedId = detail.shownId();
    /** @type {Set<string>} postings picked for a bulk action; never stored */
    this.picked = new Set();
    this.pickAnchor = null;
    /** on a narrow screen, a tap picks a row while this is on */
    this.selecting = false;
    /** rows removed or put back since the last render, drawn together by flushRender */
    this.pending = null;
    this.opened = openedIds();
    this.settings = null;
    this.mounted = true;
    this.painted = false;
    this.pinning = false;
    this.openQueue = openNextQueue();
    this.cleanups = [];
    this.search = debounce((text) => this.set({ q: text }, { replace: true }), SEARCH_DELAY_MS);
    /** @type {{key: string, counts: Promise<Record<string, Record<string, number>>>} | null} */
    this.facetCache = null;
    /** the open filter panel that shows facet counts */
    this.countedPanel = null;
    this.prefetchFacets = debounce(() => this.facets().then(() => {
      if (this.countedPanel?.isConnected) this.paintCounts(this.countedPanel);
    }, () => {
      // an open panel says the counts failed when it asks for them again
    }), SEARCH_DELAY_MS);
  }

  mount() {
    this.build();
    this.cleanups.push(
      () => {
        this.mounted = false;
        this.token += 1;
      },
      register(this.shortcuts()),
      addCommands((query) => this.commands(query)),
      detail.onApplicationChange((job) => this.apply(job)),
      subscribe("application_changed", (event) => this.onRemoteChange(event)),
      subscribe("run_done", (event) => this.onRunDone(event)),
      subscribe("feedback_changed", ({ id }) => this.onFeedback(id)),
      subscribe("scores_explained", ({ id }) => this.onReasons(id)),
      subscribe("links_done", () => this.onLinksDone()),
      subscribe("icons_progress", () => this.onIcons()),
      subscribe("icons_done", () => this.onIcons()),
      this.ctx.onStatus(() => this.onStatus()),
      () => this.observer.disconnect(),
      () => this.search.cancel(),
      () => this.prefetchFacets.cancel(),
      () => closePopover(),
      detail.setNavigator({
        position: (id) => {
          const index = this.order.findIndex((row) => row.id === id);
          return index < 0 ? null : { index: index + 1, total: this.total };
        },
        step: (delta) => this.move(delta, { focus: false }),
        keys: { prev: "k", next: "j" },
      }),
    );
    this.reload();
    return () => this.cleanups.forEach((fn) => fn());
  }

  // structure
  build() {
    this.count = h("span", { class: "view-count", "aria-live": "polite" });
    this.searchInput = h("input", { type: "search", class: "search-input",
      placeholder: "Search company, title, requirements", "aria-label": "Search postings",
      "aria-keyshortcuts": "/", value: this.filters.q,
      oninput: () => this.search(this.searchInput.value) });
    this.filterBar = h("div", { class: "filter-bar", role: "toolbar", "aria-label": "Filters" });
    this.banner = h("div", { class: "banner", role: "status", hidden: true });
    this.bulkBar = h("div", { class: "bulk-bar", role: "toolbar", "aria-label": "Selected postings", hidden: true });
    this.sentinel = h("div", { class: "list-foot" });
    this.list = h("div", { class: "list", role: "listbox", "aria-label": `${this.scope.title} postings`,
      "aria-multiselectable": "true", tabindex: "-1", onclick: (event) => this.onListClick(event),
      onmousedown: (event) => event.shiftKey && event.preventDefault() });
    this.subline = this.scope.byRun ? h("p", { class: "view-sub mono" }) : null;
    this.openNextLabel = h("span");
    this.openNext = this.scope.byRun ? h("button", { type: "button", class: "btn btn-sm open-next", hidden: true,
      title: "Opens the top five postings you have not opened yet, one tab per click",
      onclick: () => this.openNextPosting() }, icon("external"), this.openNextLabel) : null;
    this.selectToggle = h("button", { type: "button", class: "btn btn-sm select-toggle", "aria-pressed": "false",
      title: "Click rows to select them for Pass, Save, or Applied", onclick: () => this.toggleSelecting() }, "Select");
    this.root.replaceChildren(h("section", { class: "view jobs-view" },
      h("header", { class: "view-head" },
        h("div", { class: "view-heading" }, h("h1", { class: "display", text: this.scope.title }), this.count,
          this.openNext, this.selectToggle),
        h("div", { class: "search" }, icon("search"), this.searchInput,
          h("kbd", { class: "search-kbd", text: "/" })),
        this.subline),
      this.filterBar, h("div", { class: "list-wrap" }, this.bulkBar, this.banner, this.list)));
    this.observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) this.loadMore();
    }, { root: this.list, rootMargin: "400px 0px" });
    this.renderFilterBar();
    this.renderSubline();
  }

  renderSubline() {
    if (!this.subline) return;
    const run = this.ctx.latestRun();
    const counts = run?.counts || {};
    this.subline.hidden = !run;
    if (!run) return;
    this.subline.textContent = [["fetched", counts.total], ["new", counts.new], ["ranked", counts.ranked],
      ["summarized", counts.summarised]].filter(([, n]) => n != null)
      .map(([label, n]) => `${label} ${fmt.number(n)}`)
      .concat(`started ${fmt.when(run.started_at)}`).join(" · ");
  }

  shortcuts() {
    const group = "Jobs";
    const selected = () => this.rows.find((row) => row.id === this.selectedId);
    const act = (status) => {
      if (this.picked.size) {
        this.bulkStatus(status);
        return true;
      }
      const job = selected();
      if (job) setStatus(job, status);
      return Boolean(job);
    };
    return [
      ...postingShortcuts({ group, selected, move: (step) => this.move(step), act }),
      { keys: ["J"], label: "Select the next posting too", group,
        run: (event) => (event.shiftKey ? this.extend(1) : this.move(1)) },
      { keys: ["K"], label: "Select the previous posting too", group,
        run: (event) => (event.shiftKey ? this.extend(-1) : this.move(-1)) },
      { keys: ["Escape"], label: "Clear the selection", group, noPalette: true, run: () => {
        if (!this.picked.size || this.ctx.overlayOpen()) return false;
        this.clearPicks();
        return true;
      } },
      { keys: ["/"], label: "Search", group, whileTyping: true, run: (event) => {
        if (event.key === "/" && isTyping(event.target)) return false;
        this.searchInput.focus();
        this.searchInput.select();
        return true;
      } },
      { keys: ["Escape"], label: "Clear search", group, whileTyping: true, hidden: true,
        run: (event) => {
          if (event.target !== this.searchInput) return false;
          if (this.searchInput.value) {
            this.searchInput.value = "";
            this.search.cancel();
            this.set({ q: "" }, { replace: true });
          } else {
            this.searchInput.blur();
          }
          return true;
        } },
    ];
  }

  /** Palette entries: pin the filters and, on Jobs, search for the typed text. */
  commands(query) {
    const hash = `#jobs?${writeFilters(this.filters)}`;
    const pinnable = this.scope === SCOPES.jobs && narrowed(this.filters) && !this.ctx.isPinned(hash)
      && !this.ctx.pinsFull();
    return [
      pinnable && { id: "jobs:pin", label: "Pin this view", group: "Jobs", run: () => this.startPin() },
      this.scope === SCOPES.jobs && query && { id: "jobs:search", label: `Search: ${query}`, group: "Jobs",
        last: true, run: (text) => {
          this.searchInput.value = text;
          this.search.cancel();
          this.set({ q: text });
        } },
    ].filter(Boolean);
  }

  // filters
  set(changes, { replace = false } = {}) {
    this.filters = { ...this.filters, ...changes };
    this.ctx.setParams(writeFilters(this.filters), { replace });
    if ("q" in changes && this.searchInput.value !== this.filters.q) {
      this.searchInput.value = this.filters.q;
    }
    this.renderFilterBar();
    this.reload();
    this.prefetchFacets();
  }

  /** The API filter params for the filters, with `overrides` over them. */
  params(overrides = {}) {
    const f = { ...this.filters, ...overrides };
    const fixed = this.scope.fixed;
    return { q: f.q.trim(), relevant_only: fixed.rel ?? f.rel, fit_min: f.fit, tier_min: f.tier,
      status: fixed.status || f.status, hide_passed: !f.passed, category: f.category,
      location: f.loc, source: f.source, posted_within_days: f.posted, sponsorship: f.sponsor,
      salary_min: f.pay, run_id: this.scope.byRun ? this.runId : null };
  }

  query(offset, overrides = {}) {
    return qs({ ...this.params(overrides), sort: overrides.sort ?? this.filters.sort,
      limit: overrides.limit ?? PAGE, offset });
  }

  /** Counts per source, category, status and sponsorship under the filters, fetched once per filter state. */
  facets() {
    const key = qs(this.params());
    if (this.facetCache?.key !== key) {
      const counts = api(`/api/jobs/facets${key}`, { quiet: true });
      this.facetCache = { key, counts };
      counts.catch(() => {
        if (this.facetCache?.counts === counts) this.facetCache = null;
      });
    }
    return this.facetCache.counts;
  }

  async paintCounts(panel) {
    this.countedPanel = panel;
    let counts;
    try {
      counts = await this.facets();
    } catch (error) {
      if (panel.isConnected && !panel.querySelector(".pop-error")) {
        panel.append(h("p", { class: "pop-hint pop-error", text: `Couldn't count the postings: ${error.message}` }));
      }
      return;
    }
    for (const cell of panel.querySelectorAll("[data-facet]")) {
      const found = counts[cell.dataset.facet] || {};
      cell.textContent = fmt.number(found[cell.dataset.value] ?? 0);
    }
  }

  matches(job) {
    const status = this.scope.fixed.status || this.filters.status;
    if (status.length && !status.includes(job.status ?? "none")) return false;
    return !(job.status === "passed" && !this.filters.passed && !status.includes("passed"));
  }

  renderFilterBar() {
    const focused = this.filterBar.contains(document.activeElement)
      ? /** @type {HTMLElement} */ (document.activeElement).dataset.key : null;
    const f = this.filters;
    const hidden = this.scope.hidden;
    const statusText = f.status.map((s) => (s === "none" ? "No status" : STATUS_LABELS[s])).join(", ");
    const picked = (name, values, describe) => {
      if (values.length > 1) return `${name} · ${values.length}`;
      return values.length ? `${name}: ${describe(values[0])}` : name;
    };
    const items = [
      !hidden.has("rel") && switchControl("Matches my preferences", f.rel, (on) => this.set({ rel: on })),
      this.chip("Fit", f.fit != null ? `Fit ≥ ${f.fit}` : "Fit ≥", f.fit != null,
        () => thresholdPanel(this, "fit", "fit"), () => this.set({ fit: null })),
      this.chip("Tier", f.tier != null ? `Tier ≥ ${f.tier}` : "Tier ≥", f.tier != null,
        () => thresholdPanel(this, "tier", "company tier"), () => this.set({ tier: null })),
      this.chip("Pay", f.pay != null ? `Pay ≥ ${payLabel(f.pay)}` : "Min pay", f.pay != null,
        () => payPanel(this), () => this.set({ pay: null })),
      this.chip("Source", picked("Source", f.source, (name) => describeSource(name).label),
        f.source.length > 0, () => sourcePanel(this), () => this.set({ source: [] })),
      this.chip("Category", picked("Category", f.category, (name) => name),
        f.category.length > 0, () => categoryPanel(this), () => this.set({ category: [] })),
      this.chip("Sponsorship", f.sponsor ? `Sponsorship: ${f.sponsor === "yes" ? "Yes" : "No"}` : "Sponsorship",
        f.sponsor != null, () => sponsorPanel(this), () => this.set({ sponsor: null })),
      this.chip("Location", f.loc ? `Location: ${f.loc}` : "Location", Boolean(f.loc),
        () => locationPanel(this), () => this.set({ loc: "" })),
      this.chip("Posted", f.posted ? `Posted: ${f.posted} days` : "Posted", f.posted != null,
        () => postedPanel(this), () => this.set({ posted: null })),
      !hidden.has("status") && this.chip("Status", f.status.length ? `Status: ${statusText}` : "Status",
        f.status.length > 0, () => statusPanel(this), () => this.set({ status: [] })),
      !hidden.has("passed") && switchControl("Hide passed", !f.passed, (on) => this.set({ passed: !on })),
    ];
    const select = (label, options, value, onChange) => h("label", { class: "select-wrap" },
      h("span", { class: "select-label", text: label }),
      h("select", { class: "select", "data-key": label, onchange: (e) => onChange(e.target.value) },
        options.map(([key, text]) => h("option", { value: key, selected: key === value, text }))));
    this.filterBar.replaceChildren(...[...items,
      h("span", { class: "filter-spacer" }),
      this.scope === SCOPES.jobs && narrowed(f) && this.pinControl(),
      narrowed(f) && h("button", { type: "button", class: "link-btn", text: "Reset", "data-key": "reset",
        onclick: () => this.reset() }),
      select("Sort", SORTS, f.sort, (sort) => this.set({ sort })),
      select("Group", GROUPS, this.group, (mode) => {
        this.group = mode;
        setPref(this.scope.group[0], mode);
        this.renderList();
      })].filter(Boolean));
    if (focused) this.refocusFilter(focused);
  }

  /** Focus the re-rendered control that had focus, or the nearest sensible stand-in. */
  refocusFilter(key) {
    const find = (k) => this.filterBar.querySelector(`[data-key="${CSS.escape(k)}"]`);
    const target = find(key) || (key.startsWith("clear:") && find(`chip:${key.slice(6)}`))
      || this.rowEls.get(this.selectedId) || this.list;
    target.focus({ preventScroll: true });
  }

  /** "Pin this view", or the inline field that names the pin. */
  pinControl() {
    const full = this.ctx.pinsFull();
    if (this.ctx.isPinned(`#jobs?${writeFilters(this.filters)}`)) {
      return h("button", { type: "button", class: "btn btn-sm", "data-key": "pin", disabled: true,
        title: "You already pinned these filters in the sidebar" }, icon("check"), "Pinned");
    }
    if (!this.pinning) {
      return h("button", { type: "button", class: "btn btn-sm", "data-key": "pin", disabled: full,
        title: full ? "The sidebar holds six pinned views. Unpin one first" : "Keep these filters in the sidebar under Jobs",
        onclick: () => this.startPin() }, icon("pin"), "Pin this view");
    }
    const name = h("input", { type: "text", class: "input pin-name", maxlength: String(PIN_NAME_MAX),
      placeholder: "Name this view", "aria-label": "Pinned view name", "data-key": "pin-name",
      onkeydown: (event) => {
        if (event.key !== "Escape") return;
        event.preventDefault();
        this.pinning = false;
        this.renderFilterBar();
        this.filterBar.querySelector('[data-key="pin"]')?.focus();
      } });
    return h("form", { class: "pin-form", onsubmit: (event) => {
      event.preventDefault();
      const text = name.value.trim();
      if (!text) {
        name.focus();
        return;
      }
      this.ctx.addPin(text, `#jobs?${writeFilters(this.filters)}`);
      toast(`Pinned “${text}” under Jobs`);
      this.pinning = false;
      this.renderFilterBar();
    } }, name, h("button", { type: "submit", class: "btn btn-primary btn-sm", text: "Pin" }),
    h("button", { type: "button", class: "link-btn", text: "Cancel", onclick: () => {
      this.pinning = false;
      this.renderFilterBar();
    } }));
  }

  startPin() {
    this.pinning = true;
    this.renderFilterBar();
    this.filterBar.querySelector(".pin-name")?.focus();
  }

  reset() {
    this.searchInput.value = "";
    this.set({ ...DEFAULTS, sort: this.filters.sort });
  }

  /** A filter chip, reused across renders so an open popover keeps its anchor. */
  chip(name, label, active, build, clear) {
    let chip = this.chips.get(name);
    if (!chip) {
      const button = h("button", { type: "button", "aria-haspopup": "dialog", "aria-expanded": "false",
        "data-key": `chip:${name}` });
      button.addEventListener("click", () => togglePopover(button, () => chip.build(),
        { label: `${name} filter` }));
      chip = { button, wrap: h("span", {}), build };
      this.chips.set(name, chip);
    }
    chip.build = build;
    chip.button.className = `filter-chip${active ? " active" : ""}`;
    chip.button.replaceChildren(...[label, !active && icon("chevronDown")].filter(Boolean));
    chip.wrap.className = `filter-chip-wrap${active ? " active" : ""}`;
    chip.wrap.replaceChildren(...[chip.button, active && h("button", { type: "button",
      class: "filter-chip-clear", "data-key": `clear:${name}`, title: "Clear",
      "aria-label": `Clear the ${name.toLowerCase()} filter`, onclick: clear }, icon("x"))].filter(Boolean));
    return chip.wrap;
  }

  // data
  async reload() {
    const token = ++this.token;
    if (this.scope.byRun && this.runId == null) {
      this.loading = false;
      this.list.removeAttribute("aria-busy");
      this.rows = [];
      this.total = 0;
      this.renderList();
      return;
    }
    this.loading = true;
    this.list.setAttribute("aria-busy", "true");
    try {
      const page = await api(`/api/jobs${this.query(0)}`);
      if (token !== this.token) return;
      this.rows = page.rows;
      this.total = page.total;
      this.removed.clear();
      this.banner.hidden = true;
      this.renderList();
      this.list.scrollTop = 0;
    } catch {
      if (token === this.token) this.renderError();
    } finally {
      if (token === this.token) {
        this.loading = false;
        this.list.removeAttribute("aria-busy");
      }
    }
  }

  async loadMore() {
    if (this.loading || this.rows.length >= this.total) return false;
    const token = this.token;
    this.loading = true;
    try {
      const page = await api(`/api/jobs${this.query(this.rows.length)}`);
      if (token !== this.token) return false;
      const known = new Set(this.rows.map((row) => row.id));
      const fresh = page.rows.filter((row) => !known.has(row.id));
      this.rows.push(...fresh);
      this.total = page.total;
      this.renderCount();
      this.appendRows(fresh);
      this.renderFoot();
      this.observe();
      return true;
    } catch {
      return false;
    } finally {
      if (token === this.token) this.loading = false;
    }
  }

  /**
   * Bring the list in line with a posting's new application state. Rows that leave
   * or come back are redrawn together once the current task ends, so a bulk change
   * renders the list once.
   */
  apply(job) {
    const index = this.rows.findIndex((row) => row.id === job.id);
    if (index < 0) {
      const gone = this.removed.get(job.id);
      if (gone && this.matches(job)) this.restore(job, gone);
      else if (this.scope.fixed.status && this.matches(job)) this.reload();
      return;
    }
    if (this.matches(job)) {
      this.rows[index] = job;
      const old = this.rowEls.get(job.id);
      const focused = old?.contains(document.activeElement);
      const row = this.rowEl(job);
      old?.replaceWith(row);
      if (focused) row.focus({ preventScroll: true });
      return;
    }
    this.rows.splice(index, 1);
    this.total = Math.max(0, this.total - 1);
    this.queueRender().removed.push({ id: job.id, index });
  }

  /** Put back a row that left the list, as an Undo does, at its old place. */
  restore(job, gone) {
    this.removed.delete(job.id);
    this.rows.splice(Math.min(gone.index, this.rows.length), 0, job);
    this.total += 1;
    this.queueRender().restored.push({ id: job.id, next: gone.next });
  }

  queueRender() {
    if (!this.pending) {
      this.pending = { removed: [], restored: [], hadFocus: this.list.contains(document.activeElement) };
      queueMicrotask(() => this.flushRender());
    }
    return this.pending;
  }

  flushRender() {
    const { removed, restored, hadFocus } = this.pending;
    this.pending = null;
    if (!this.mounted) return;
    const before = this.order.map((row) => row.id);
    if (restored.length || !this.rows.length) this.renderList();
    else this.dropRows(removed.map((row) => row.id));
    const selectedGone = removed.some((row) => row.id === this.selectedId);
    let next = null;
    if (selectedGone) {
      const at = before.indexOf(this.selectedId);
      const kept = (id) => this.rowEls.has(id);
      next = before.slice(at + 1).find(kept) ?? before.slice(0, Math.max(at, 0)).reverse().find(kept) ?? null;
      const was = this.selectedId;
      if (next) this.select(next, { focus: hadFocus });
      else {
        detail.clear();
        if (hadFocus) this.list.focus({ preventScroll: true });
      }
      removed.find((row) => row.id === was).next = next;
    }
    for (const row of removed) this.removed.set(row.id, { index: row.index, next: row.next ?? null });
    const back = restored.find((row) => row.next && row.next === this.selectedId);
    if (back) this.select(back.id, { focus: hadFocus });
  }

  async onRemoteChange({ id, status }) {
    const row = this.rows.find((known) => known.id === id);
    if (!row && !this.scope.fixed.status) return;
    if (row && (row.status ?? null) === (status ?? null)) return;
    try {
      const job = await api(`/api/jobs/${encodeURIComponent(id)}`, { quiet: true });
      if (this.mounted) this.apply(job);
    } catch {
      // a posting deleted meanwhile keeps its stale row until the next reload
    }
  }

  async onFeedback(id) {
    const row = this.rows.find((known) => known.id === id);
    if (!row) return;
    try {
      const job = await api(`/api/jobs/${encodeURIComponent(id)}`, { quiet: true });
      if (this.mounted && (job.feedback?.verdict ?? null) !== (row.feedback?.verdict ?? null)) this.apply(job);
    } catch {
      // a posting deleted meanwhile keeps its stale row until the next reload
    }
  }

  async onReasons(id) {
    const row = this.rows.find((known) => known.id === id);
    if (!row || row.fit_reason) return;
    try {
      const job = await api(`/api/jobs/${encodeURIComponent(id)}`, { quiet: true });
      if (this.mounted) this.apply(job);
    } catch {
      // a posting deleted meanwhile keeps its stale row until the next reload
    }
  }

  /** Mark the loaded rows that a link check found closed, or open again. */
  async onLinksDone() {
    if (!this.rows.length) return;
    const token = this.token;
    try {
      const page = await api(`/api/jobs${this.query(0, { limit: Math.min(this.rows.length, LIMIT_MAX) })}`, { quiet: true });
      if (token !== this.token || !this.mounted) return;
      const closed = new Map(page.rows.map((row) => [row.id, row.closed]));
      for (const row of [...this.rows]) {
        if (closed.has(row.id) && closed.get(row.id) !== row.closed) this.apply({ ...row, closed: closed.get(row.id) });
      }
    } catch {
      // the closed marks catch up on the next reload
    }
  }

  /** Swap monograms for icons that a fetch pass just stored, keeping the selection. */
  async onIcons() {
    if (!this.rows.some((row) => !row.logo_domain)) return;
    const token = this.token;
    try {
      const page = await api(`/api/jobs${this.query(0, { limit: Math.min(this.rows.length, LIMIT_MAX) })}`, { quiet: true });
      if (token !== this.token || !this.mounted) return;
      const domains = new Map(page.rows.map((row) => [row.id, row.logo_domain]));
      for (const row of this.rows) {
        const domain = domains.get(row.id);
        if (!domain || domain === row.logo_domain) continue;
        row.logo_domain = domain;
        this.rowEls.get(row.id)?.querySelector(".avatar")?.replaceWith(avatar(row, 24));
        detail.refreshAvatar(row.id, domain);
      }
    } catch {
      // icons are decoration; a failed refresh leaves the monograms until the next reload
    }
  }

  onStatus() {
    const runId = this.ctx.latestRun()?.id ?? null;
    if (runId === this.runId) return;
    this.runId = runId;
    if (this.scope.byRun) {
      this.renderSubline();
      this.reload();
    } else if (this.rows.length) {
      this.renderList();
    }
  }

  onRunDone({ status, counts }) {
    if (narrowed(this.filters) || this.scope !== SCOPES.jobs) return;
    const dry = status === "dry-run";
    const n = dry ? counts?.new : counts?.ranked;
    const text = dry ? `Dry run finished · ${fmt.plural(n ?? 0, "new posting")} found`
      : `${fmt.plural(n ?? 0, "new posting")} ranked`;
    this.banner.replaceChildren(icon("latest"), h("span", { text }),
      h("button", { type: "button", class: "btn btn-sm", text: "Show", onclick: () => this.reload() }),
      h("button", { type: "button", class: "icon-btn", "aria-label": "Dismiss",
        onclick: () => { this.banner.hidden = true; } }, icon("x")));
    this.banner.hidden = false;
  }

  // bulk selection
  async bulkStatus(next) {
    const jobs = this.rows.filter((row) => this.picked.has(row.id));
    this.clearPicks();
    await detail.changeStatuses(jobs, next);
  }

  setPicked(id, on) {
    if (on) this.picked.add(id);
    else this.picked.delete(id);
  }

  /** Mark every drawn row picked or not and redraw the bulk bar. */
  showPicks() {
    for (const [id, row] of this.rowEls) this.markRow(row, id);
    this.list.classList.toggle("selecting", this.selecting || this.picked.size > 0);
    this.renderBulkBar();
  }

  /** aria-selected holds for the picked rows, or for the current row while none is picked. */
  markRow(row, id) {
    const picked = this.picked.has(id);
    row.classList.toggle("picked", picked);
    row.setAttribute("aria-selected", String(this.picked.size ? picked : id === this.selectedId));
  }

  clearPicks() {
    this.picked.clear();
    this.pickAnchor = null;
    this.showPicks();
  }

  toggleSelecting(on = !this.selecting) {
    this.selecting = on;
    this.selectToggle.setAttribute("aria-pressed", String(on));
    this.selectToggle.textContent = on ? "Done" : "Select";
    if (on) this.showPicks();
    else this.clearPicks();
  }

  /** Pick every row from the anchor to this one. The anchor is the first row picked, else the selected row. */
  pickRange(id) {
    this.pickAnchor ??= this.selectedId;
    const ids = this.order.map((row) => row.id);
    const [from, to] = [ids.indexOf(this.pickAnchor), ids.indexOf(id)];
    if (from < 0) this.setPicked(id, true);
    else ids.slice(Math.min(from, to), Math.max(from, to) + 1).forEach((known) => this.setPicked(known, true));
    this.select(id, { focus: true, keepPicks: true });
    this.showPicks();
  }

  async extend(step) {
    if (this.selectedId && this.rowEls.has(this.selectedId)) {
      this.pickAnchor ??= this.selectedId;
      this.setPicked(this.selectedId, true);
    }
    await this.move(step, { keepPicks: true });
    if (this.selectedId) this.setPicked(this.selectedId, true);
    this.showPicks();
    return true;
  }

  renderBulkBar() {
    const n = this.picked.size;
    this.bulkBar.hidden = n === 0;
    if (!n) return;
    const action = (label, status, key, title) => h("button", { type: "button", class: "btn btn-sm",
      title: `${title} (${key})`, "aria-keyshortcuts": key.toLowerCase(), onclick: () => this.bulkStatus(status) }, label);
    this.bulkBar.replaceChildren(
      h("span", { class: "bulk-count", "aria-live": "polite", text: `${fmt.number(n)} selected` }),
      action("Pass", "passed", "X", "Pass on every selected posting"),
      action("Save", "saved", "S", "Save every selected posting"),
      action("Applied", "applied", "A", "Mark every selected posting applied"),
      h("button", { type: "button", class: "link-btn", text: "Clear", title: "Clear the selection (Esc)",
        onclick: () => this.clearPicks() }));
  }

  // list
  renderCount() {
    const rel = this.scope.fixed.rel ?? this.filters.rel;
    const noun = this.scope === SCOPES.saved ? "saved"
      : (rel ? (this.total === 1 ? "match" : "matches") : (this.total === 1 ? "posting" : "postings"));
    this.count.textContent = `${fmt.number(this.total)} ${noun}`;
  }

  renderList() {
    const present = new Set(this.rows.map((row) => row.id));
    for (const id of this.picked) if (!present.has(id)) this.picked.delete(id);
    this.rowEls.clear();
    this.groups = [];
    this.groupsByLabel.clear();
    this.order = [];
    this.list.replaceChildren(...[!this.rows.length && this.emptyState(), this.sentinel].filter(Boolean));
    this.appendRows(this.rows);
    this.renderAroundRows();
    if (!this.painted && this.rows.length) this.fadeIn();
  }

  /** Take rows that left this.rows out of the rendered list; the other rows stay as they are. */
  dropRows(ids) {
    const gone = new Set(ids);
    for (const id of gone) {
      this.rowEls.get(id)?.remove();
      this.rowEls.delete(id);
      this.picked.delete(id);
    }
    for (const group of this.groups) {
      group.rows = group.rows.filter((job) => !gone.has(job.id));
      if (group.rows.length) continue;
      group.head?.remove();
      this.groupsByLabel.delete(group.label);
    }
    this.groups = this.groups.filter((group) => group.rows.length);
    this.order = this.groups.flatMap((group) => group.rows);
    this.renderGroupCounts();
    detail.refreshNav();
    this.renderAroundRows();
  }

  /** The parts of the view that follow the loaded rows: selection, counts, foot, and Open next. */
  renderAroundRows() {
    this.list.classList.toggle("selecting", this.selecting || this.picked.size > 0);
    this.renderBulkBar();
    this.renderCount();
    this.renderFoot();
    this.observe();
    this.openQueue.reset();
    this.renderOpenNext();
  }

  /** Fade in the view's first list top down; rows past FADE_ROWS share the last delay. */
  fadeIn() {
    this.painted = true;
    this.order.forEach((job, i) => {
      const row = this.rowEls.get(job.id);
      row?.classList.add("row-enter");
      row?.style.setProperty("--i", String(Math.min(i, FADE_ROWS)));
    });
  }

  renderOpenNext() {
    if (!this.openNext) return;
    const left = this.openQueue.candidates(this.order, this.opened).length;
    this.openNext.hidden = !this.rows.length;
    this.openNext.disabled = left === 0;
    this.openNextLabel.textContent = left ? `Open next (${left} left)` : "Top picks opened";
  }

  openNextPosting() {
    const job = this.openQueue.open(this.order, this.opened);
    if (job) this.markOpened(job.id);
  }

  markOpened(id) {
    this.opened.add(id);
    markOpened(id);
    this.rowEls.get(id)?.querySelector(".row-dot")?.classList.remove("unread");
    this.renderOpenNext();
  }

  /** Add rows to the rendered list, each at the end of its group; earlier rows stay as they are. */
  appendRows(rows) {
    const labelOf = GROUP_LABELS[this.group] || GROUP_LABELS.none;
    for (const job of rows) {
      const label = labelOf(job);
      const group = this.groupsByLabel.get(label) ?? this.addGroup(label);
      const row = this.rowEl(job);
      const last = group.rows.length ? this.rowEls.get(group.rows[group.rows.length - 1].id) : group.head;
      if (last) last.after(row);
      else this.list.insertBefore(row, this.sentinel);
      group.rows.push(job);
    }
    this.renderGroupCounts();
    this.order = this.groups.flatMap((group) => group.rows);
    detail.refreshNav();
  }

  /** A group that later pages can still add to shows its count with a "+". */
  renderGroupCounts() {
    const more = this.rows.length < this.total;
    const last = this.groups[this.groups.length - 1];
    const inOrder = this.group === "date" && this.filters.sort === "newest";
    for (const group of this.groups) {
      if (!group.count) continue;
      const growing = more && (!inOrder || group === last);
      group.count.textContent = `${fmt.number(group.rows.length)}${growing ? "+" : ""}`;
    }
  }

  addGroup(label) {
    const count = label == null ? null : h("span", { class: "mono" });
    const head = label == null ? null
      : h("div", { class: "group-head", role: "presentation" }, h("span", { text: label }), count);
    const group = { label, rows: [], head, count };
    const rank = (other) => BUCKET_ORDER.indexOf(other);
    let index = this.group === "date" ? this.groups.findIndex((g) => rank(g.label) > rank(label)) : -1;
    if (index < 0) index = this.groups.length;
    this.groups.splice(index, 0, group);
    this.groupsByLabel.set(label, group);
    if (head) this.list.insertBefore(head, this.groups[index + 1]?.head ?? this.sentinel);
    return group;
  }

  observe() {
    this.observer.disconnect();
    if (this.rows.length < this.total) this.observer.observe(this.sentinel);
  }

  renderFoot() {
    const more = this.rows.length < this.total;
    this.sentinel.replaceChildren(...[this.rows.length > 0 && h("span", { class: "muted",
      text: `Showing ${fmt.number(this.rows.length)} of ${fmt.number(this.total)}` }),
    more && h("button", { type: "button", class: "btn btn-sm", text: "Load more",
      onclick: () => this.loadMore() })].filter(Boolean));
  }

  renderError() {
    this.count.textContent = "";
    this.list.replaceChildren(this.empty("Couldn't load postings",
      "Cairn didn't answer. Try again, or reopen the app.", "Try again", () => this.reload()));
  }

  /** An empty state; `explain` adds the why-nothing link while Jobs keeps to the preferences. */
  empty(headline, hint, action, run, art, explain = false) {
    const button = action && h("button", { type: "button", class: "btn btn-primary", text: action });
    button?.addEventListener("click", () => run(button));
    const why = explain && this.scope === SCOPES.jobs && this.filters.rel && this.whyButton();
    return h("div", { class: "empty-state" }, art && pixelArt(art),
      h("p", { class: "empty-title display", text: headline }),
      h("p", { class: "empty-hint", text: hint }), button, why);
  }

  whyButton() {
    const button = h("button", { type: "button", class: "link-btn why-button", "aria-haspopup": "dialog",
      "aria-expanded": "false", text: "See what the filters hide" });
    button.addEventListener("click", () => togglePopover(button, () => this.whyPanel(),
      { label: "What your preferences hide", className: "why-popover" }));
    return button;
  }

  whyPanel() {
    const list = h("ol", { class: "why-list" }, h("li", { class: "pop-loading", text: "Counting…" }));
    api("/api/insights/why", { quiet: true }).then((rows) => {
      list.replaceChildren(...rows.map(({ rule, dropped }) => {
        const [key, label] = WHY_RULES[rule] || [null, rule];
        const text = [h("span", { class: "why-rule", text: label }),
          h("span", { class: "why-count mono", text: fmt.number(dropped) })];
        return h("li", { class: dropped ? "" : "why-none" }, key
          ? h("a", { class: "why-link", href: `#settings?${new URLSearchParams({ section: "preferences", field: key })}`,
            title: `Open ${label} in Settings` }, text)
          : h("span", { class: "why-link" }, text));
      }));
      list.querySelector("a")?.focus({ preventScroll: true });
    }, (error) => {
      list.replaceChildren(h("li", { class: "pop-hint", text: "Couldn't count what the filters hide. Try again." }));
    });
    return h("div", { class: "pop-form" },
      h("p", { class: "pop-title", text: "Hidden in the last 30 days" }),
      h("p", { class: "pop-hint", text: "Each count skips postings already hidden by the lines above it. Click one to change it in Settings." }),
      list);
  }

  emptyState() {
    const f = this.filters;
    if (f.q.trim()) {
      return this.empty(`No results for “${f.q.trim()}”`,
        "Search looks at company names, titles, and summarized requirements.", "Clear search",
        () => this.set({ q: "" }), "magnifier", true);
    }
    const onlyDefaults = !narrowed({ ...f, q: "" });
    if (!onlyDefaults) {
      return this.empty("No matches", "The filters leave nothing. Loosen one or reset them all.",
        "Reset filters", () => this.reset(), "tray", true);
    }
    if (this.scope === SCOPES.saved) {
      return this.empty("Nothing saved yet", "Press S on a posting in Jobs to keep it here.",
        "Go to Jobs", () => this.ctx.navigate("jobs"), "tray");
    }
    if (!this.ctx.latestRun()) {
      return this.empty("Nothing ranked yet",
        "Start a run to fetch today's postings and rank them for you.",
        "Run now", (button) => this.ctx.openRunPopover(button), "sun");
    }
    if (this.scope.byRun) {
      return this.empty("This run ranked nothing",
        "It found no new postings that match your preferences.", "Go to Jobs",
        () => this.ctx.navigate("jobs"), "sun");
    }
    return this.empty("No matches",
      "No posting matches your preferences right now.", "Show all postings",
      () => this.set({ rel: false }), "tray", true);
  }

  rowEl(job) {
    const unread = job.run_id != null && job.run_id === this.runId && !this.opened.has(job.id);
    const row = jobRow(job, { selected: job.id === this.selectedId, unread });
    this.markRow(row, job.id);
    this.rowEls.set(job.id, row);
    return row;
  }

  onListClick(event) {
    const row = event.target.closest(".row");
    const job = row && this.rows.find((known) => known.id === row.dataset.id);
    if (!job) return;
    const action = event.target.closest("[data-action]")?.dataset.action;
    if (!action && this.selecting && selectModeQuery.matches) {
      this.setPicked(job.id, !this.picked.has(job.id));
      this.showPicks();
    } else if (!action && event.shiftKey) this.pickRange(job.id);
    else if (!action && (event.metaKey || event.ctrlKey)) {
      if (openPosting(job)) this.markOpened(job.id);
    } else if (!action) this.select(job.id, { focus: true });
    else if (action === "open") openPosting(job);
    else setStatus(job, action);
  }

  select(id, { focus = false, keepPicks = false } = {}) {
    if (!keepPicks && this.picked.size) this.clearPicks();
    const previousId = this.selectedId;
    const previous = this.rowEls.get(previousId);
    this.selectedId = id;
    if (previous) {
      previous.removeAttribute("aria-current");
      activateRow(previous, false);
      this.markRow(previous, previousId);
    }
    const row = this.rowEls.get(id);
    if (row) {
      row.setAttribute("aria-current", "true");
      activateRow(row, true);
      this.markRow(row, id);
      row.querySelector(".row-dot")?.classList.remove("unread");
      row.scrollIntoView({ block: "nearest" });
      if (focus || this.list.contains(document.activeElement)) row.focus({ preventScroll: true });
    }
    this.opened.add(id);
    this.renderOpenNext();
    detail.show(id, { reveal: !keepPicks });
  }

  async move(step, { focus = true, keepPicks = false } = {}) {
    if (!this.order.length) return false;
    const current = () => this.order.findIndex((row) => row.id === this.selectedId);
    let index = current();
    if (index < 0) index = step > 0 ? -1 : this.order.length;
    // appended rows join their groups, which can move the selected row
    if (index + step >= this.order.length && await this.loadMore()) index = current();
    const next = Math.max(0, Math.min(this.order.length - 1, index + step));
    this.select(this.order[next].id, { focus, keepPicks });
    return true;
  }
}

/**
 * The Jobs, Saved and Latest run views: one filtered, paged posting list.
 * @param {HTMLElement} root
 * @param {object} ctx  see app.js `viewContext`
 * @returns {() => void} unmount
 */
export function mount(root, ctx) {
  return new JobsList(root, ctx, SCOPES[ctx.view] ? ctx.view : "jobs").mount();
}
