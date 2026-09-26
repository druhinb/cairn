import { api } from "../lib/api.js";
import { fmt, h } from "../lib/dom.js";
import { describeSource, kindLabel, sourceDot, specName } from "../components/chip.js";
import { STATUS_LABELS, statusPill } from "../components/pill.js";
import { closePopover } from "../components/popover.js";

const STATUSES = Object.keys(STATUS_LABELS);
export const SORTS = [["score", "Best match"], ["newest", "Newest"], ["company", "Company"],
  ["updated", "Recently updated"], ["salary", "Pay"]];
const POSTED = [[7, "7 days"], [14, "14 days"], [30, "30 days"]];
const QUICK = { fit: [60, 70, 80], tier: [40, 55, 70] };
const QUICK_PAY_K = [80, 100, 120, 150];
const PAY_MAX = 10000000;
const SPONSORSHIP = [[null, "Any"], ["yes", "Yes"], ["no", "No"]];

export const DEFAULTS = { q: "", rel: true, fit: null, tier: null, pay: null, source: [], category: [],
  sponsor: null, loc: "", posted: null, status: [], passed: false, sort: "score" };

/** The filter state a hash's params describe; anything unrecognised is dropped. */
export function readFilters(params) {
  const int = (key, low, high) => {
    const n = Number.parseInt(params.get(key) ?? "", 10);
    return Number.isFinite(n) ? Math.max(low, Math.min(high, n)) : null;
  };
  const sort = params.get("sort");
  const sponsor = params.get("sponsor");
  return {
    q: params.get("q") || "",
    rel: params.get("rel") !== "0",
    fit: int("fit", 0, 100),
    tier: int("tier", 0, 100),
    pay: int("pay", 0, PAY_MAX),
    sponsor: sponsor === "yes" || sponsor === "no" ? sponsor : null,
    source: [...new Set(params.getAll("source").filter(Boolean))],
    category: [...new Set(params.getAll("category").filter(Boolean))],
    loc: params.get("loc") || "",
    posted: int("posted", 1, 3650),
    status: (params.get("status") || "").split(",")
      .filter((s) => s === "none" || STATUSES.includes(s)),
    passed: params.get("passed") === "1",
    sort: SORTS.some(([value]) => value === sort) ? sort : "score",
  };
}

export function writeFilters(f) {
  const params = new URLSearchParams();
  if (f.q) params.set("q", f.q);
  if (!f.rel) params.set("rel", "0");
  for (const key of ["fit", "tier", "pay", "posted"]) if (f[key] != null) params.set(key, String(f[key]));
  for (const key of ["source", "category"]) for (const value of f[key]) params.append(key, value);
  if (f.sponsor) params.set("sponsor", f.sponsor);
  if (f.loc) params.set("loc", f.loc);
  if (f.status.length) params.set("status", f.status.join(","));
  if (f.passed) params.set("passed", "1");
  if (f.sort !== "score") params.set("sort", f.sort);
  return params;
}

export function narrowed(f) {
  return writeFilters({ ...f, sort: "score" }).toString() !== "";
}

/** "$120k" for a yearly amount in dollars. */
export function payLabel(dollars) {
  return `$${fmt.number(Math.round(dollars / 100) / 10)}k`;
}

function pick(list, changes) {
  closePopover();
  list.set(changes);
}

export function thresholdPanel(list, key, noun) {
  const current = list.filters[key];
  const input = h("input", { type: "number", class: "input input-num", min: "0", max: "100",
    step: "1", value: current ?? "", "aria-label": `Minimum ${noun}` });
  const apply = (value) => {
    const n = Number.parseInt(value, 10);
    pick(list, { [key]: Number.isFinite(n) ? Math.max(0, Math.min(100, n)) : null });
  };
  return h("form", { class: "pop-form", onsubmit: (e) => {
    e.preventDefault();
    apply(input.value);
  } },
  h("p", { class: "pop-title", text: `Minimum ${noun}` }),
  h("div", { class: "pop-row" }, input, h("button", { type: "submit", class: "btn btn-primary btn-sm", text: "Apply" })),
  h("div", { class: "pop-row" }, QUICK[key].map((value) => h("button", { type: "button",
    class: `quick${current === value ? " active" : ""}`, text: `${value}+`, onclick: () => apply(value) }))));
}

/** A count cell that paintCounts fills from `facet`; none without a facet or for the "any" choice. */
function countCell(facet, value) {
  return facet && value != null && h("span", { class: "pop-count mono", "data-facet": facet, "data-value": value });
}

function radioPanel(list, title, options, current, onPick, facet) {
  const name = `pick-${title.toLowerCase().replace(/\W+/g, "-")}`;
  const panel = h("fieldset", { class: "pop-form pop-list" }, h("legend", { class: "pop-title", text: title }),
    options.map(({ value, label, hint }) => h("label", { class: "pop-option" },
      h("input", { type: "radio", name, checked: value === current, onchange: () => onPick(value) }),
      h("span", { class: "pop-option-text" }, label, hint && h("span", { class: "pop-hint", text: hint })),
      countCell(facet, value))));
  if (facet) list.paintCounts(panel);
  return panel;
}

function checkPanel(list, title, options, current, onChange, facet) {
  const chosen = new Set(current);
  const panel = h("fieldset", { class: "pop-form pop-list" }, h("legend", { class: "pop-title", text: title }),
    options.map(({ value, label, hint }) => h("label", { class: "pop-option" },
      h("input", { type: "checkbox", checked: chosen.has(value), onchange: (e) => {
        if (e.target.checked) chosen.add(value);
        else chosen.delete(value);
        onChange(options.map((option) => option.value).filter((v) => chosen.has(v)));
      } }),
      h("span", { class: "pop-option-text" }, label, hint && h("span", { class: "pop-hint", text: hint })),
      countCell(facet, value))));
  if (facet) list.paintCounts(panel);
  return panel;
}

function loadSettings(list) {
  list.settings ??= api("/api/settings").catch((error) => {
    list.settings = null;
    throw error;
  });
  return list.settings;
}

/** Swap the loading placeholder for the panel, focusing it if the popover is still open. */
function fillPanel(placeholder, panel) {
  if (!placeholder.isConnected) return;
  placeholder.replaceWith(panel);
  if (!panel.contains(document.activeElement)) panel.querySelector("input, button")?.focus();
}

export function sourcePanel(list) {
  const panel = h("div", { class: "pop-loading", text: "Loading sources…" });
  fillSourcePanel(list, panel);
  return panel;
}

/** The facet's values with a count, most postings first; a failed count leaves them out. */
async function facetValues(list, facet) {
  const counts = await list.facets().catch(() => ({}));
  return Object.keys(counts[facet] || {}).filter((value) => value !== "none");
}

async function fillSourcePanel(list, panel) {
  let cfg;
  try {
    cfg = await loadSettings(list);
  } catch {
    panel.textContent = "Couldn't load the sources.";
    return;
  }
  const names = [...new Set([...(cfg.sources || []), ...(cfg.watchlist || [])].map(specName)
    .concat(await facetValues(list, "source"), list.filters.source))];
  const kinds = [...new Set(names.map((name) => describeSource(name).kind))];
  const options = names.map((name) => {
    const { kind, label } = describeSource(name);
    return { value: name, label: h("span", { class: "pop-source" }, sourceDot(kind), label), hint: name };
  });
  fillPanel(panel, h("div", {},
    checkPanel(list, "Source", options, list.filters.source, (source) => list.set({ source }), "source"),
    h("p", { class: "pop-legend" }, kinds.map((kind) => h("span", {}, sourceDot(kind), kindLabel(kind))))));
}

export function categoryPanel(list) {
  const panel = h("div", { class: "pop-loading", text: "Loading categories…" });
  Promise.all([loadSettings(list), facetValues(list, "category")]).then(([cfg, counted]) => {
    const names = [...new Set([...(cfg.allowed_categories || []), ...counted, ...list.filters.category])];
    fillPanel(panel, checkPanel(list, "Category", names.map((name) => ({ value: name, label: name })),
      list.filters.category, (category) => list.set({ category }), "category"));
  }, () => {
    panel.textContent = "Couldn't load the categories.";
  });
  return panel;
}

export function sponsorPanel(list) {
  return radioPanel(list, "Offers visa sponsorship", SPONSORSHIP.map(([value, label]) => ({ value, label })),
    list.filters.sponsor, (sponsor) => pick(list, { sponsor }), "sponsorship");
}

export function payPanel(list) {
  const current = list.filters.pay;
  const input = h("input", { type: "number", class: "input input-num", min: "0", step: "1",
    value: current == null ? "" : String(Math.round(current / 1000)), "aria-label": "Minimum yearly pay in thousands of dollars" });
  const apply = (thousands) => {
    const n = Number.parseFloat(thousands);
    pick(list, { pay: Number.isFinite(n) && n > 0 ? Math.min(PAY_MAX, Math.round(n * 1000)) : null });
  };
  return h("form", { class: "pop-form", onsubmit: (e) => {
    e.preventDefault();
    apply(input.value);
  } },
  h("p", { class: "pop-title", text: "Minimum yearly pay" }),
  h("div", { class: "pop-row" }, h("span", { class: "pop-unit", text: "$" }), input,
    h("span", { class: "pop-unit", text: "k" }),
    h("button", { type: "submit", class: "btn btn-primary btn-sm", text: "Apply" })),
  h("div", { class: "pop-row" }, QUICK_PAY_K.map((k) => h("button", { type: "button",
    class: `quick${current === k * 1000 ? " active" : ""}`, text: `$${k}k+`, onclick: () => apply(k) }))),
  h("p", { class: "pop-hint", text: "Uses the top of each posted range, in US dollars. Postings that don't list pay are hidden." }));
}

export function locationPanel(list) {
  const input = h("input", { type: "text", class: "input", value: list.filters.loc,
    placeholder: "Seattle, Remote, NY…", "aria-label": "Location contains" });
  return h("form", { class: "pop-form", onsubmit: (e) => {
    e.preventDefault();
    pick(list, { loc: input.value.trim() });
  } },
  h("p", { class: "pop-title", text: "Location contains" }),
  h("div", { class: "pop-row" }, input, h("button", { type: "submit", class: "btn btn-primary btn-sm", text: "Apply" })));
}

export function postedPanel(list) {
  return radioPanel(list, "Posted within",
    [{ value: null, label: "Any time" }, ...POSTED.map(([value, label]) => ({ value, label }))],
    list.filters.posted, (posted) => pick(list, { posted }));
}

export function statusPanel(list) {
  const options = [...STATUSES.map((value) => ({ value, label: statusPill(value) })),
    { value: "none", label: "No status" }];
  return checkPanel(list, "Status", options, list.filters.status, (status) => list.set({ status }), "status");
}
