import { h } from "../lib/dom.js";

/** @param {number} value @returns {"good" | "ok" | "weak"} */
export function band(value) {
  if (value >= 80) return "good";
  if (value >= 60) return "ok";
  return "weak";
}

/**
 * Ten 4×6 blocks, one lit per ten points in the band colour, with the number beside
 * them. A missing value renders a dash; `belowFloor` hatches the lit blocks grey and
 * says why.
 * @param {number | null | undefined} value
 * @param {{label: string, belowFloor?: boolean}} options
 * @returns {HTMLElement}
 */
export function meter(value, { label, belowFloor = false }) {
  if (value == null) {
    return h("span", { class: "meter meter-none", title: `${label}: not ranked` },
      h("span", { class: "meter-label", text: label }),
      h("span", { class: "meter-value", text: "–" }));
  }
  const tone = belowFloor ? "floor" : band(value);
  const lit = Math.round(Math.max(0, Math.min(100, value)) / 10);
  return h("span", { class: `meter meter-${tone}`,
    title: belowFloor ? `${label} ${value}: below your minimum` : `${label} ${value}` },
  h("span", { class: "meter-label", text: label }),
  h("span", { class: "meter-track", "aria-hidden": "true" },
    h("span", { class: "meter-fill", style: `--lit: ${lit}` })),
  h("span", { class: "meter-value", text: String(value) }));
}
