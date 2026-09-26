import { h } from "../lib/dom.js";
import { icon } from "./icons.js";
import { menu, togglePopover } from "./popover.js";
import { STATUS_LABELS } from "./pill.js";

const SEGMENTS = [["saved", "Save", "s"], ["applied", "Applied", "a"],
  ["interviewing", "Interviewing", null], ["offer", "Offer", null]];
const MORE = ["rejected", "withdrawn", "passed"];

/**
 * The status segmented control: Save · Applied · Interviewing · Offer, with a menu
 * for Rejected, Withdrawn, Pass, Clear status and any `extra` actions. Clicking the
 * current segment leaves it set.
 * @param {string | null} status
 * @param {(status: string | null) => void} onChange
 * @param {{extra?: {label: string, run: () => void}[]}} [options]
 * @returns {HTMLElement}
 */
export function statusControl(status, onChange, { extra = [] } = {}) {
  const segments = SEGMENTS.map(([value, label, key]) => h("button", {
    type: "button", class: `segment status-${value}`, "aria-pressed": String(status === value),
    "data-key": value,
    title: key ? `${label} (${key.toUpperCase()})` : label,
    onclick: () => status !== value && onChange(value),
  }, label));
  const moreLabel = MORE.includes(status) ? STATUS_LABELS[status] : null;
  const more = h("button", { type: "button",
    class: `segment segment-more ${moreLabel ? `status-${status}` : ""}`, "data-key": "more",
    "aria-pressed": String(Boolean(moreLabel)), "aria-haspopup": "menu", "aria-expanded": "false",
    title: "More statuses and actions" }, moreLabel || icon("more", "More statuses and actions"),
  moreLabel && icon("chevronDown"));
  more.addEventListener("click", () => togglePopover(more, () => menu([
    ...MORE.filter((value) => value !== status).map((value) => ({
      label: value === "passed" ? "Pass (X)" : STATUS_LABELS[value],
      run: () => onChange(value), tone: value === "rejected" ? "weak" : null })),
    ...(status ? [{ label: "Clear status", run: () => onChange(null) }] : []),
    ...extra,
  ]), { label: "More statuses and actions", align: "end" }));
  return h("div", { class: "segmented status-control", role: "group", "aria-label": "Status" },
    segments, more);
}
