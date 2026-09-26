import { h } from "../lib/dom.js";
import { icon } from "./icons.js";

/**
 * A row of numbered steps: the ones before `current` done, `current` active, the
 * rest to come. `current` equal to the number of steps marks them all done;
 * `failed` marks the active step as where it stopped.
 * @param {string[]} labels
 * @param {number} current
 * @param {{label: string, failed?: boolean}} options
 * @returns {HTMLElement}
 */
export function stepper(labels, current, { label, failed = false }) {
  return h("ol", { class: "stepper", "aria-label": label }, labels.map((text, i) => {
    const state = i < current ? "done" : i === current ? (failed ? "failed" : "active") : "todo";
    return h("li", { class: `step step-${state}`, "aria-current": i === current ? "step" : null },
      h("span", { class: "step-mark", "aria-hidden": "true" },
        state === "done" ? icon("check") : state === "failed" ? icon("x") : String(i + 1)),
      h("span", { class: "step-label", text }));
  }));
}
