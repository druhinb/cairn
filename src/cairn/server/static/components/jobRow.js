import { fmt, h } from "../lib/dom.js";
import { avatar } from "./avatar.js";
import { categoryChip, termChip, timingChip } from "./chip.js";
import { icon } from "./icons.js";
import { meter } from "./meter.js";
import { statusPill } from "./pill.js";
import { place } from "./popover.js";

const SENT = new Set(["applied", "interviewing", "offer", "rejected", "withdrawn"]);
const VERDICTS = { up: ["thumbUp", "You agreed with this score"],
  down: ["thumbDown", "You marked this score too high"] };
const TIP_ID = "score-tip";
const NO_REASON = "No reason recorded yet. Open the posting and choose Explain these scores.";

/** @type {{panel: HTMLElement, anchor: HTMLElement, gone: MutationObserver} | null} */
let tip = null;

function hideTip() {
  if (!tip) return;
  tip.anchor.removeAttribute("aria-describedby");
  tip.panel.remove();
  tip.gone.disconnect();
  tip = null;
  window.removeEventListener("scroll", hideTip, true);
}

/** @param {HTMLElement} anchor @param {string} title @param {string | null} reason */
function showTip(anchor, title, reason) {
  if (tip?.anchor === anchor) return;
  hideTip();
  const panel = h("div", { class: "popover", role: "tooltip", id: TIP_ID },
    h("p", { class: "pop-title", text: title }),
    h("p", { class: reason ? null : "pop-hint", text: reason || NO_REASON }));
  document.body.append(panel);
  place(panel, anchor, "end");
  anchor.setAttribute("aria-describedby", TIP_ID);
  // a list redraw can take the badge away without a mouseleave or blur
  const gone = new MutationObserver(() => !anchor.isConnected && hideTip());
  gone.observe(document.body, { childList: true, subtree: true });
  tip = { panel, anchor, gone };
  window.addEventListener("scroll", hideTip, true);
}

/**
 * A fit or tier meter whose reason shows while it is hovered or focused.
 * @param {number | null | undefined} value @param {string} name  "Fit" or "Company tier"
 * @param {string | null} reason @param {{label: string, belowFloor?: boolean, selected: boolean}} options
 */
function scoreBadge(value, name, reason, { label, belowFloor = false, selected }) {
  const badge = meter(value, { label, belowFloor });
  if (value == null) return badge;
  badge.removeAttribute("title");
  badge.classList.add("row-score");
  badge.tabIndex = selected ? 0 : -1;
  return reasonOnHover(badge, `${name} ${value}${belowFloor ? " · below your floor" : ""}`, reason);
}

/**
 * Show a score's reason in a tip while the anchor is hovered or focused.
 * @template {HTMLElement} T @param {T} anchor @param {string} title @param {string | null} reason
 * @returns {T}
 */
export function reasonOnHover(anchor, title, reason) {
  const show = () => showTip(anchor, title, reason);
  anchor.addEventListener("mouseenter", show);
  anchor.addEventListener("focus", show);
  anchor.addEventListener("mouseleave", () => document.activeElement !== anchor && hideTip());
  anchor.addEventListener("blur", () => !anchor.matches(":hover") && hideTip());
  anchor.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || tip?.anchor !== anchor) return;
    event.stopPropagation();
    hideTip();
  });
  return anchor;
}

/** "Seattle, WA +2" for a posting's locations, or null when it lists none. */
export function locationText(locations) {
  if (!locations?.length) return null;
  return locations.length > 1 ? `${locations[0]} +${locations.length - 1}` : locations[0];
}

function dayText(isoDay) {
  // a bare "YYYY-MM-DD" parses as UTC midnight, the previous day west of Greenwich
  const date = new Date(`${isoDay}T00:00`);
  if (Number.isNaN(date.getTime())) return isoDay;
  const year = date.getFullYear() === new Date().getFullYear() ? undefined : "numeric";
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric", year });
}

/**
 * The posting's age, and under it how long ago Cairn saw it appear when that was in
 * the last day. The backlog of a source's first read has no found_at.
 */
function rowAge(job) {
  const found = fmt.since(job.found_at);
  const posted = fmt.full(job.posted_at);
  const title = [posted && `Posted ${posted}`, found && `Cairn saw it appear ${found} ago`].filter(Boolean);
  return h("span", { class: "row-age mono", title: title.join(". ") || null },
    h("span", { text: fmt.age(job.posted_at) }),
    found && h("span", { class: "row-found", text: `found ${found}` }));
}

/**
 * One posting as a two-line list row: company and title, then location and chips;
 * on the right the verdict, pay, fit and tier, status and age, and the hover actions
 * (buttons with `data-action` open, saved, applied or passed). The caller sets
 * aria-selected and listens for clicks; a selected row is the list's one tab stop.
 * @param {object} job  an /api/jobs row
 * @param {{selected?: boolean, unread?: boolean}} [options]
 * @returns {HTMLElement}
 */
export function jobRow(job, { selected = false, unread = false } = {}) {
  const repeat = job.applied_before && !SENT.has(job.status);
  const loc = locationText(job.locations);
  const pay = fmt.pay(job.salary);
  const verdict = VERDICTS[job.feedback?.verdict];
  const action = (name, label, value) => h("button", { type: "button", class: "icon-btn",
    tabindex: selected ? "0" : "-1", title: label, "aria-label": label, "data-action": value }, icon(name));
  return h("div", { class: `row${job.closed ? " row-closed" : ""}`, role: "option",
    "aria-current": selected ? "true" : null, tabindex: selected ? "0" : "-1", "data-id": job.id },
  h("span", { class: "row-lead" },
    h("span", { class: `row-dot${unread ? " unread" : ""}`,
      title: unread ? "The latest run ranked this and you have not opened it" : null }),
    h("span", { class: "row-check", "aria-hidden": "true" }, icon("check"))),
  avatar(job, 24),
  h("div", { class: "row-text" },
    h("div", { class: "row-line1" },
      h("span", { class: "row-company", text: job.company || "–", "data-company": job.company || null,
        "data-logo": job.logo_domain || null }),
      h("span", { class: "row-sep", "aria-hidden": "true", text: "·" }),
      h("span", { class: "row-title", text: job.title })),
    h("div", { class: "row-line2" },
      job.closed && h("span", { class: "pill pill-closed", text: "Closed",
        title: "The last link check found this posting closed" }),
      job.reposts > 0 && h("span", { class: "pill pill-repost", text: job.reposts > 1 ? `Reposted ${job.reposts}×` : "Reposted",
        title: `${job.company || "The company"} took this role down and posted it again. A role that keeps coming back may not be hiring.` }),
      loc && h("span", { class: "row-loc", text: loc, title: (job.locations || []).join("\n") }),
      job.timing && timingChip(job.timing, job.starts_before_graduation),
      repeat && h("span", { class: "row-repeat", text: `applied before · ${dayText(job.applied_before)}`,
        title: `You applied to ${job.company || "this company"} on ${dayText(job.applied_before)}` }),
      job.terms.length > 0 && termChip(job.terms), job.category && categoryChip(job.category))),
  h("div", { class: "row-right" },
    verdict && h("span", { class: `row-verdict verdict-${job.feedback.verdict}`, title: verdict[1] },
      icon(verdict[0], verdict[1])),
    pay && h("span", { class: "row-pay mono", text: pay, title: `Stated pay: ${pay}` }),
    scoreBadge(job.fit, "Fit", job.fit_reason, { label: "fit", selected }),
    scoreBadge(job.tier, "Company tier", job.tier_reason,
      { label: "tier", belowFloor: Boolean(job.below_floor), selected }),
    h("div", { class: "row-tail" },
      h("span", { class: "row-status" }, job.status && statusPill(job.status)),
      rowAge(job),
      h("div", { class: "row-actions" },
        action("external", "Open posting (Enter)", "open"),
        action("saved", "Save (S)", "saved"),
        action("applied", "Mark applied (A)", "applied"),
        action("pass", "Pass (X)", "passed")))));
}

/**
 * Make a row the list's tab stop, with its score badges and action buttons after it
 * in the tab order, or take it out of the tab order.
 * @param {HTMLElement} row @param {boolean} on
 */
export function activateRow(row, on) {
  row.tabIndex = on ? 0 : -1;
  for (const stop of row.querySelectorAll(".row-score, .row-actions button")) stop.tabIndex = on ? 0 : -1;
}
