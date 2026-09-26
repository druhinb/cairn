import { api } from "../lib/api.js";
import { h } from "../lib/dom.js";
import { subscribe } from "../lib/events.js";

const PALETTE_SIZE = 8;
// monograms drawn within this long of each other are asked about in one request
const WANT_DELAY_MS = 250;
const WANT_BATCH = 200;

/** @type {Map<string, string>} icon domains found since the page loaded, by company name */
const found = new Map();
const asked = new Set();
/** @type {string[]} */
let wanted = [];
let wantTimer = null;

function sendWanted() {
  const companies = wanted.slice(0, WANT_BATCH);
  wanted = wanted.slice(WANT_BATCH);
  wantTimer = wanted.length ? setTimeout(sendWanted, WANT_DELAY_MS) : null;
  // icons are decoration; a refused request leaves the monograms in place
  api("/api/icons/wanted", { method: "POST", body: { companies }, quiet: true }).catch(() => {});
}

/** Ask the server, once per page load, to fetch the icon of a company shown without one. */
function want(name) {
  if (!name || asked.has(name)) return;
  asked.add(name);
  wanted.push(name);
  wantTimer ??= setTimeout(sendWanted, WANT_DELAY_MS);
}

/** "Jane Street" → "JS", "Stripe" → "S", "" → "?". */
function initials(name) {
  const words = String(name || "").split(/\s+/).map((word) => word.match(/[\p{L}\p{N}]/u)?.[0])
    .filter(Boolean);
  if (!words.length) return "?";
  return (words.length === 2 ? words.join("") : words[0]).toUpperCase();
}

// djb2, so a company keeps its colour across reloads and views
function hue(name) {
  let hash = 5381;
  for (const ch of String(name || "").toLowerCase()) hash = ((hash * 33) ^ ch.codePointAt(0)) >>> 0;
  return (hash % PALETTE_SIZE) + 1;
}

function monogram(name, size) {
  return h("span", { class: `avatar avatar-${size} avatar-mono`, "aria-hidden": "true",
    style: `--avatar-bg: var(--avatar-${hue(name)})`, text: initials(name) });
}

/**
 * A company's icon from the app server, or its monogram when the company has no
 * stored icon or the icon fails to load. Decorative: the company name sits beside it.
 * A monogram for a company with no stored icon asks the server to fetch one, and
 * turns into the icon when the server reports it.
 * @param {{company?: string, logo_domain?: string | null}} job
 * @param {24 | 48} [size]
 * @returns {HTMLElement}
 */
export function avatar(job, size = 24) {
  const name = job.company || "";
  const domain = job.logo_domain || found.get(name);
  if (!domain) {
    want(name);
    const mono = monogram(name, size);
    Object.assign(mono.dataset, { company: name, size: String(size) });
    return mono;
  }
  const img = h("img", { src: `/api/logos/${encodeURIComponent(domain)}`, alt: "",
    loading: "lazy", decoding: "async" });
  const tile = h("span", { class: `avatar avatar-${size} avatar-img`, "aria-hidden": "true" }, img);
  img.addEventListener("error", () => tile.replaceWith(monogram(name, size)), { once: true });
  return tile;
}

// a monogram drawn for a company with no icon takes the icon the server finds for it
subscribe("icons_found", ({ icons }) => {
  for (const [name, domain] of Object.entries(icons || {})) found.set(name, domain);
  for (const mono of document.querySelectorAll(".avatar-mono[data-company]")) {
    const { company, size } = /** @type {HTMLElement} */ (mono).dataset;
    if (found.has(company)) mono.replaceWith(avatar({ company }, size === "48" ? 48 : 24));
  }
});
