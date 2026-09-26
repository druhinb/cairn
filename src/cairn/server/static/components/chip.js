import { h } from "../lib/dom.js";

const BOARD_LABELS = { greenhouse: "Greenhouse", lever: "Lever", ashby: "Ashby",
  smartrecruiters: "SmartRecruiters", workable: "Workable", bamboohr: "BambooHR", workday: "Workday",
  hn_hiring: "HN Who is hiring", yc_waas: "Y Combinator", remoteok: "RemoteOK", usajobs: "USAJOBS",
  page: "Careers page" };

/** @param {string} kind a config source kind @returns {string} its display name */
export function kindLabel(kind) {
  if (kind === "github") return "GitHub feed";
  return kind === "github_readme" ? "GitHub README" : BOARD_LABELS[kind] || kind;
}

/**
 * The kind and short label of a stored source name: "owner/repo" for a GitHub feed,
 * "owner/repo/file" for a README list other than README.md, "kind:location" otherwise.
 * @param {string} name @returns {{kind: string, label: string}}
 */
export function describeSource(name) {
  const text = String(name || "");
  const at = text.indexOf(":");
  const kind = at < 0 ? "" : text.slice(0, at);
  if (BOARD_LABELS[kind]) return { kind, label: BOARD_LABELS[kind] };
  const repo = text.split("/").pop();
  return { kind: "github", label: repo || "unknown" };
}

/**
 * The stored source name of a config `[[sources]]` or `[[watchlist]]` entry, as
 * cairn.sources.Source.name derives it.
 * @param {{kind: string, location: string}} spec @returns {string}
 */
export function specName(spec) {
  if (spec.kind !== "github" && spec.kind !== "github_readme") return `${spec.kind}:${spec.location}`;
  try {
    const parts = new URL(spec.location).pathname.split("/").filter(Boolean);
    if (spec.kind === "github") return parts.slice(0, 2).join("/");
    // raw README urls run owner/repo/branch/path
    const file = parts.slice(3).join("/");
    return file === "README.md" ? parts.slice(0, 2).join("/") : `${parts.slice(0, 2).join("/")}/${file}`;
  } catch {
    // an unparsable feed url names itself
    return spec.location;
  }
}

/** The dot that marks a source kind, coloured by kind. @param {string} kind @returns {HTMLElement} */
export function sourceDot(kind) {
  return h("span", { class: `chip-dot source-${kind}`, "aria-hidden": "true" });
}

/** @param {string} name @returns {HTMLElement} */
export function sourceChip(name) {
  const { kind, label } = describeSource(name);
  return h("span", { class: "chip chip-source", title: name },
    sourceDot(kind), h("span", { class: "chip-text", text: label }));
}

const SEASONS = new Set(["winter", "spring", "summer", "fall"]);

/**
 * An internship's season, as "Summer 2027 +1" when the posting lists more than one.
 * @param {string[]} terms @returns {HTMLElement}
 */
export function termChip(terms) {
  const season = terms[0].split(" ")[0].toLowerCase();
  const text = terms.length > 1 ? `${terms[0]} +${terms.length - 1}` : terms[0];
  return h("span", { class: "chip chip-term", title: terms.join("\n") },
    h("span", { class: `chip-dot${SEASONS.has(season) ? ` season-${season}` : ""}`, "aria-hidden": "true" }),
    h("span", { class: "chip-text", text }));
}

const CATEGORY_TINTS = { "Software": "blue", "AI/ML/Data": "violet", "AI/ML": "violet", "Quant": "ok" };

/** @param {string} category @returns {HTMLElement} */
export function categoryChip(category) {
  const tint = CATEGORY_TINTS[category] || "plain";
  return h("span", { class: `chip chip-category category-${tint}`, title: category },
    h("span", { class: "chip-text", text: category }));
}

/**
 * @param {string} timing @param {boolean} beforeGraduation
 * @returns {HTMLElement}
 */
export function timingChip(timing, beforeGraduation) {
  const [tone, text] = beforeGraduation ? ["chip-weak", "starts before you graduate"] : ["chip-ok", timing];
  return h("span", { class: `chip chip-timing ${tone}`, title: timing },
    h("span", { class: "chip-text", text }));
}
