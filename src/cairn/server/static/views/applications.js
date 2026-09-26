import { api, qs } from "../lib/api.js";
import { debounce, fmt, h } from "../lib/dom.js";
import { subscribe } from "../lib/events.js";
import { register } from "../lib/keys.js";
import { pref, setPref } from "../lib/store.js";
import { avatar } from "../components/avatar.js";
import { icon } from "../components/icons.js";
import { meter } from "../components/meter.js";
import { pixelArt } from "../components/pixelart.js";
import { STATUS_LABELS, statusPill } from "../components/pill.js";
import { closePopover, menu, openPopover } from "../components/popover.js";
import { exportCsv, importCard } from "./applicationsCsv.js";
import * as detail from "./detail.js";
import { STAGE_LABELS } from "./detailTracking.js";

const COLUMNS = [
  { key: "saved", title: "Saved", statuses: ["saved"] },
  { key: "applied", title: "Applied", statuses: ["applied"] },
  { key: "interviewing", title: "Interviewing", statuses: ["interviewing"] },
  { key: "offer", title: "Offer", statuses: ["offer"] },
  { key: "closed", title: "Closed", statuses: ["rejected", "withdrawn"] },
];
const TRACKED = COLUMNS.flatMap((column) => column.statuses);
const text = (value) => String(value || "").toLowerCase();
const TABLE = [
  { key: "status", label: "Status", value: (row) => TRACKED.indexOf(row.status) },
  { key: "company", label: "Company", value: (row) => text(row.company) },
  { key: "title", label: "Title", value: (row) => text(row.title) },
  { key: "fit", label: "Fit", value: (row) => row.fit ?? -1, numeric: true },
  { key: "tier", label: "Tier", value: (row) => row.tier ?? -1, numeric: true },
  { key: "applied_at", label: "Applied on", value: (row) => row.applied_at || "" },
  { key: "updated_at", label: "Updated", value: (row) => row.updated_at || "" },
  { key: "note", label: "Note", value: (row) => text(row.note) },
];
const MODES = [["board", "Board", "board"], ["list", "List", "list"]];
const RELOAD_DELAY_MS = 250;

function columnOf(status) {
  return COLUMNS.findIndex((column) => column.statuses.includes(status));
}

function firstLine(note) {
  return String(note || "").split("\n").find((line) => line.trim()) || "";
}

class Applications {
  constructor(root, ctx) {
    this.root = root;
    this.ctx = ctx;
    this.rows = [];
    this.counts = {};
    this.loaded = false;
    this.only = null;
    this.mode = pref("appsMode", "board") === "list" ? "list" : "board";
    this.sort = pref("appsSort", { key: "updated_at", desc: true });
    this.selectedId = null;
    this.refocus = null;
    this.dragging = null;
    this.renderQueued = false;
    this.mounted = true;
    this.cleanups = [];
    this.reloadSoon = debounce(() => this.load(), RELOAD_DELAY_MS);
  }

  mount() {
    this.build();
    this.cleanups.push(
      () => { this.mounted = false; },
      register(this.shortcuts()),
      detail.onApplicationChange((job) => this.apply(job)),
      subscribe("application_changed", () => this.reloadSoon()),
      subscribe("tracking_changed", () => this.reloadSoon()),
      subscribe("reconnected", () => this.reloadSoon()),
      subscribe("icons_done", () => this.reloadSoon()),
      () => this.reloadSoon.cancel(),
      () => closePopover(),
      detail.setNavigator({
        position: (id) => {
          const order = this.ordered();
          const index = order.findIndex((row) => row.id === id);
          return index < 0 ? null : { index: index + 1, total: order.length };
        },
        step: (delta) => {
          const order = this.ordered();
          const index = order.findIndex((row) => row.id === this.selectedId);
          const next = order[Math.max(0, Math.min(order.length - 1, index + delta))];
          if (next) this.select(next.id);
        },
      }),
    );
    this.load();
    return () => this.cleanups.forEach((fn) => fn());
  }

  build() {
    this.count = h("span", { class: "view-count", "aria-live": "polite" });
    this.pills = h("div", { class: "apps-pills", role: "group", "aria-label": "Show one status" });
    this.modeButtons = MODES.map(([value, label, name]) => h("button", { type: "button",
      class: "segment", "aria-pressed": String(this.mode === value), "data-mode": value,
      onclick: () => this.setMode(value) }, icon(name), label));
    this.body = h("div", { class: "apps-body" });
    this.importSlot = h("div", { class: "import-slot" });
    this.fileInput = h("input", { type: "file", class: "import-input", accept: ".csv,text/csv", hidden: true,
      "aria-label": "CSV file to import", onchange: () => this.openImport() });
    this.importButton = h("button", { type: "button", class: "btn btn-sm",
      title: "Set statuses and notes from a CSV file. You see a preview before anything changes",
      onclick: () => this.fileInput.click() }, icon("upload"), "Import CSV");
    this.root.replaceChildren(h("section", { class: "view apps-view" },
      h("header", { class: "view-head" },
        h("div", { class: "view-heading" }, h("h1", { class: "display", text: "Applications" }), this.count),
        h("div", { class: "apps-tools" },
          h("button", { type: "button", class: "btn btn-sm", title: "Download every application as a CSV file",
            onclick: () => exportCsv() }, icon("download"), "Export CSV"),
          this.importButton, this.fileInput,
          h("div", { class: "segmented", role: "group", "aria-label": "Layout" }, this.modeButtons))),
      h("div", { class: "apps-bar" }, this.pills),
      this.importSlot,
      this.body));
  }

  async openImport() {
    const file = this.fileInput.files?.[0];
    this.fileInput.value = "";
    if (!file) return;
    const close = () => {
      this.importSlot.replaceChildren();
      this.importButton.focus();
    };
    this.importSlot.replaceChildren(h("p", { class: "import-card muted", text: `Reading ${file.name}…` }));
    const card = await importCard(file, close);
    if (!this.mounted) return;
    this.importSlot.replaceChildren(card);
    card.querySelector(".btn-primary:not(:disabled), button")?.focus();
  }

  shortcuts() {
    const group = "Applications";
    const move = (step) => () => {
      const card = document.activeElement?.closest?.(".card");
      const row = card && this.rows.find((known) => known.id === card.dataset.id);
      if (!row) return false;
      const target = columnOf(row.status) + step;
      if (target < 0 || target >= COLUMNS.length) return true;
      this.moveTo(row, COLUMNS[target], card);
      return true;
    };
    return [
      { keys: ["["], label: "Move the card a column left", group, run: move(-1) },
      { keys: ["]"], label: "Move the card a column right", group, run: move(1) },
    ];
  }

  // data
  async load() {
    try {
      const body = await api(`/api/applications${qs({ status: TRACKED })}`);
      if (!this.mounted) return;
      this.rows = body.rows;
      this.counts = body.counts;
      this.loaded = true;
      this.render();
    } catch {
      if (this.mounted && !this.loaded) this.renderError();
    }
  }

  /** Fold in a change made through the detail pane, before the server's event arrives. */
  apply(job) {
    const index = this.rows.findIndex((row) => row.id === job.id);
    if (index < 0) {
      this.reloadSoon();
      return;
    }
    const before = this.rows[index].status;
    if (TRACKED.includes(job.status)) {
      this.rows[index] = { ...this.rows[index], status: job.status, note: job.note,
        applied_at: job.applied_at, updated_at: job.updated_at };
    } else {
      this.rows.splice(index, 1);
    }
    if (before !== job.status) {
      if (before) this.counts[before] = Math.max(0, (this.counts[before] || 0) - 1);
      if (job.status) this.counts[job.status] = (this.counts[job.status] || 0) + 1;
    }
    this.render();
  }

  setMode(mode) {
    this.mode = mode;
    setPref("appsMode", mode);
    for (const button of this.modeButtons) {
      button.setAttribute("aria-pressed", String(button.dataset.mode === mode));
    }
    this.render();
  }

  setOnly(status) {
    this.only = this.only === status ? null : status;
    this.render();
  }

  select(id, { focus = false } = {}) {
    this.selectedId = id;
    for (const el of this.body.querySelectorAll("[data-id]")) {
      if (el.dataset.id === id) el.setAttribute("aria-current", "true");
      else el.removeAttribute("aria-current");
    }
    if (focus) this.body.querySelector(`[data-id="${CSS.escape(id)}"]`)?.focus();
    detail.show(id);
  }

  async setStatus(row, next) {
    this.refocus = row.id;
    try {
      await detail.changeStatus(row, next);
    } catch {
      this.refocus = null;
    }
  }

  moveTo(row, column, anchor) {
    if (column.statuses.includes(row.status)) return;
    if (column.key !== "closed") {
      this.setStatus(row, column.statuses[0]);
      return;
    }
    openPopover(anchor, [h("p", { class: "pop-title menu-title", text: `Close ${row.company || "this application"} as` }),
      menu([
        { label: "Rejected", tone: "weak", run: () => this.setStatus(row, "rejected") },
        { label: "Withdrawn", run: () => this.setStatus(row, "withdrawn") },
      ])], { label: "Close the application" });
  }

  // rendering
  visibleRows() {
    return this.only ? this.rows.filter((row) => row.status === this.only) : this.rows;
  }

  /** The rows in the order on screen: column by column on the board, else as sorted. */
  ordered() {
    if (this.mode === "board") {
      const rows = this.visibleRows();
      return COLUMNS.flatMap((column) => rows.filter((row) => column.statuses.includes(row.status)));
    }
    return this.sortedRows();
  }

  sortedRows() {
    const { key, desc } = this.sort;
    const spec = TABLE.find((col) => col.key === key) || TABLE[6];
    return [...this.visibleRows()].sort((a, b) => {
      const [x, y] = [spec.value(a), spec.value(b)];
      return (x < y ? -1 : x > y ? 1 : 0) * (desc ? -1 : 1);
    });
  }

  /** Redraw the body; during a drag it waits for dragend, which a removed card never gets. */
  render() {
    if (this.dragging) {
      this.renderQueued = true;
      return;
    }
    this.renderQueued = false;
    const focused = this.refocus
      ?? document.activeElement?.closest?.("[data-id]")?.dataset.id ?? null;
    const inBody = this.refocus != null || this.body.contains(document.activeElement);
    this.refocus = null;
    this.renderHeader();
    if (!this.rows.length) this.body.replaceChildren(this.emptyState());
    else this.body.replaceChildren(this.mode === "board" ? this.board() : this.table());
    if (focused && inBody) this.body.querySelector(`[data-id="${CSS.escape(focused)}"]`)?.focus();
    detail.refreshNav();
  }

  renderHeader() {
    const tracked = TRACKED.reduce((sum, status) => sum + (this.counts[status] || 0), 0);
    this.count.textContent = `${fmt.number(tracked)} tracked`;
    const focusedStatus = this.pills.contains(document.activeElement)
      ? /** @type {HTMLElement} */ (document.activeElement).dataset.status : null;
    this.pills.replaceChildren(...[...TRACKED.map((status) => h("button", { type: "button",
      class: `count-pill pill pill-${status}`, "data-status": status,
      "aria-pressed": String(this.only === status),
      title: this.only === status ? "Show every status" : `Show only ${STATUS_LABELS[status].toLowerCase()}`,
      onclick: () => this.setOnly(status) },
    STATUS_LABELS[status], h("span", { class: "count-pill-n mono", text: fmt.number(this.counts[status] || 0) }))),
    this.only && h("button", { type: "button", class: "link-btn", text: "Show all",
      onclick: () => this.setOnly(this.only) })].filter(Boolean));
    if (focusedStatus) this.pills.querySelector(`[data-status="${focusedStatus}"]`)?.focus();
  }

  renderError() {
    this.body.replaceChildren(h("div", { class: "empty-state" },
      h("p", { class: "empty-title display", text: "Couldn't load applications" }),
      h("p", { class: "empty-hint", text: "Cairn didn't answer. Try again, or reopen the app." }),
      h("button", { type: "button", class: "btn btn-primary", text: "Try again", onclick: () => this.load() })));
  }

  emptyState() {
    return h("div", { class: "empty-state" }, pixelArt("plane"),
      h("p", { class: "empty-title display", text: "No applications yet" }),
      h("p", { class: "empty-hint", text: "To start tracking, press S (save) or A (applied) on a posting in Jobs." }),
      h("button", { type: "button", class: "btn btn-primary", text: "Go to Jobs",
        onclick: () => this.ctx.navigate("jobs") }));
  }

  board() {
    const rows = this.visibleRows();
    return h("div", { class: "board" }, COLUMNS.map((column) => {
      const cards = rows.filter((row) => column.statuses.includes(row.status));
      const list = h("div", { class: "column-cards", role: "list" }, cards.map((row) => this.card(row, column)));
      const section = h("section", { class: `column column-${column.key}`, "aria-label": column.title },
        h("header", { class: "column-head" }, h("h2", { class: "column-title display", title: column.title, text: column.title }),
          h("span", { class: "column-count mono", text: String(cards.length) })),
        list);
      this.dropTarget(section, column);
      return section;
    }));
  }

  card(row, column) {
    const note = firstLine(row.note);
    const card = h("article", { class: "card", tabindex: "0", draggable: "true", role: "listitem",
      "data-id": row.id, "aria-current": row.id === this.selectedId ? "true" : null,
      "aria-keyshortcuts": "[ ]",
      "aria-label": `${row.company}, ${row.title}, ${STATUS_LABELS[row.status]}`,
      onclick: () => this.select(row.id, { focus: true }),
      onkeydown: (event) => {
        if ((event.key === "Enter" || event.key === " ") && event.target === card) {
          event.preventDefault();
          this.select(row.id);
        }
      } },
    h("div", { class: "card-head" }, avatar(row, 24),
      h("span", { class: "card-company", text: row.company || "–", "data-company": row.company || null,
        "data-logo": row.logo_domain || null })),
    h("p", { class: "card-title", text: row.title }),
    column.key === "closed" && h("div", {}, statusPill(row.status)),
    h("div", { class: "card-scores" }, meter(row.fit, { label: "fit" }),
      meter(row.tier, { label: "tier", belowFloor: Boolean(row.below_floor) })),
    this.trackingLine(row),
    h("div", { class: "card-foot" },
      h("span", { class: "card-note", text: note || "", title: row.note || null }),
      h("span", { class: "card-age mono", text: fmt.age(row.updated_at),
        title: `Updated ${fmt.full(row.updated_at)}` })));
    card.addEventListener("dragstart", (event) => {
      this.dragging = row;
      event.dataTransfer.setData("text/plain", row.id);
      event.dataTransfer.effectAllowed = "move";
      card.classList.add("dragging");
    });
    card.addEventListener("dragend", () => {
      this.dragging = null;
      card.classList.remove("dragging");
      for (const el of this.body.querySelectorAll(".drop-target")) el.classList.remove("drop-target");
      if (this.renderQueued) this.render();
    });
    return card;
  }

  /** The next stage's date and the checklist progress, when either is set. */
  trackingLine(row) {
    const next = row.next_stage;
    const list = row.checklist;
    if (!next && !list?.total) return null;
    const stage = next && (STAGE_LABELS[next.stage] || next.stage);
    return h("div", { class: "card-tracking" },
      next && h("span", { class: "card-stage", title: `${stage} ${fmt.full(next.at)}` }, icon("calendar"),
        `${stage} · ${fmt.day(next.at)}`),
      list?.total > 0 && h("span", { class: `card-checks mono${list.done === list.total ? " is-done" : ""}`,
        title: `${list.done} of ${list.total} checklist items done` }, icon("check"), `${list.done}/${list.total}`));
  }

  dropTarget(section, column) {
    const accepts = () => this.dragging && !column.statuses.includes(this.dragging.status);
    section.addEventListener("dragover", (event) => {
      if (!accepts()) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = "move";
      section.classList.add("drop-target");
    });
    section.addEventListener("dragleave", (event) => {
      if (!section.contains(/** @type {Node} */ (event.relatedTarget))) section.classList.remove("drop-target");
    });
    section.addEventListener("drop", (event) => {
      event.preventDefault();
      section.classList.remove("drop-target");
      const row = this.dragging;
      if (row && accepts()) this.moveTo(row, column, section.querySelector(".column-head"));
    });
  }

  table() {
    const { key, desc } = this.sort;
    const rows = this.sortedRows();
    const head = h("tr", {}, TABLE.map((col) => h("th", { scope: "col",
      class: col.numeric ? "num" : null,
      "aria-sort": col.key === key ? (desc ? "descending" : "ascending") : null },
    h("button", { type: "button", class: "sort-btn", "data-sort": col.key, onclick: () => this.sortBy(col.key) },
      col.label, col.key === key && icon("chevronDown")))));
    const body = rows.map((row) => {
      const tr = h("tr", { tabindex: "0", "data-id": row.id, "aria-current": row.id === this.selectedId ? "true" : null,
        onclick: () => this.select(row.id, { focus: true }),
        onkeydown: (event) => {
          if (event.key === "Enter" && event.target === tr) this.select(row.id);
        } },
      h("td", {}, statusPill(row.status)),
      h("td", { class: "cell-company" }, h("span", { class: "cell-company-inner" }, avatar(row, 24),
        h("span", { text: row.company || "–", "data-company": row.company || null, "data-logo": row.logo_domain || null }))),
      h("td", { class: "cell-title", text: row.title }),
      h("td", { class: "num" }, meter(row.fit, { label: "fit" })),
      h("td", { class: "num" }, meter(row.tier, { label: "tier", belowFloor: Boolean(row.below_floor) })),
      h("td", { class: "mono cell-date", text: fmt.day(row.applied_at), title: fmt.full(row.applied_at) }),
      h("td", { class: "mono cell-date", text: fmt.age(row.updated_at), title: fmt.full(row.updated_at) }),
      h("td", { class: "cell-note", text: firstLine(row.note), title: row.note || null }));
      return tr;
    });
    return h("div", { class: "table-wrap" }, h("table", { class: `data-table apps-table sorted-${desc ? "desc" : "asc"}` },
      h("thead", {}, head), h("tbody", {}, body)));
  }

  sortBy(key) {
    const desc = this.sort.key === key ? !this.sort.desc : ["fit", "tier", "applied_at", "updated_at"].includes(key);
    this.sort = { key, desc };
    setPref("appsSort", this.sort);
    this.render();
    this.body.querySelector(`[data-sort="${key}"]`)?.focus();
  }
}

/**
 * The Applications view: tracked postings as a board or a sortable list.
 * @param {HTMLElement} root
 * @param {object} ctx  see app.js `viewContext`
 * @returns {() => void} unmount
 */
export function mount(root, ctx) {
  return new Applications(root, ctx).mount();
}
