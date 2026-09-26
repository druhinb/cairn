import { h } from "../lib/dom.js";

/** Every application status in pipeline order, with its display label. */
export const STATUS_LABELS = {
  saved: "Saved", applied: "Applied", interviewing: "Interviewing", offer: "Offer",
  rejected: "Rejected", withdrawn: "Withdrawn", passed: "Passed",
};

/** @param {string} status @returns {HTMLElement} */
export function statusPill(status) {
  return h("span", { class: `pill pill-${status}`, text: STATUS_LABELS[status] || status });
}

const RUN_LABELS = { ok: "OK", failed: "Failed", "dry-run": "Fetch only", running: "Running",
  interrupted: "Interrupted" };

/** @param {string} status a runs row status @returns {HTMLElement} */
export function runPill(status) {
  return h("span", { class: `pill pill-run-${status}`, text: RUN_LABELS[status] || status });
}
