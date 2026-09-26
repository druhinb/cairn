import { h } from "../lib/dom.js";

/**
 * An on/off switch over a checkbox. With a null label the caller labels it, through
 * `id` and a `<label for>`.
 * @param {string | null} label
 * @param {boolean} checked
 * @param {(on: boolean) => void} onChange
 * @param {{id?: string, key?: string}} [options] `key` becomes data-key, for refocusing
 * @returns {HTMLElement}
 */
export function switchControl(label, checked, onChange, { id, key = label } = {}) {
  const input = h("input", { type: "checkbox", role: "switch", id, checked, "data-key": key,
    onchange: () => onChange(input.checked) });
  return h("label", { class: "switch" }, input, h("span", { class: "switch-track", "aria-hidden": "true" }), label);
}
