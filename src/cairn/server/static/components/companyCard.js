import { api, qs } from "../lib/api.js";
import { fmt, h } from "../lib/dom.js";
import { avatar } from "./avatar.js";
import { statusPill } from "./pill.js";

const OPEN_DELAY_MS = 400;
// time to move the pointer from the name into the card before it closes
const CLOSE_DELAY_MS = 200;
const GAP = 6;
const APPLICATIONS_TTL_MS = 10000;
const SHOWN_APPLICATIONS = 3;

let current = null;
let openTimer = null;
let closeTimer = null;
let seeAll = () => {};
let applications = null;
/** the anchor whose card Esc closed; it stays closed until the pointer or focus leaves it */
let dismissed = null;

function anchorOf(target) {
  return target instanceof Element ? target.closest("[data-company]") : null;
}

function cancelTimers() {
  clearTimeout(openTimer);
  clearTimeout(closeTimer);
}

/** Close the company card, if one is open. */
export function closeCompanyCard() {
  cancelTimers();
  if (!current) return;
  current.panel.remove();
  current.anchor.removeAttribute("aria-describedby");
  current = null;
}

function closeSoon() {
  clearTimeout(openTimer);
  clearTimeout(closeTimer);
  closeTimer = setTimeout(closeCompanyCard, CLOSE_DELAY_MS);
}

function openSoon(anchor) {
  clearTimeout(closeTimer);
  if (current?.anchor === anchor || anchor === dismissed) return;
  clearTimeout(openTimer);
  openTimer = setTimeout(() => open(anchor), OPEN_DELAY_MS);
}

function loadApplications() {
  if (!applications || Date.now() - applications.at > APPLICATIONS_TTL_MS) {
    applications = { at: Date.now(), rows: api("/api/applications", { quiet: true }).then((body) => body.rows) };
    // fill() reports the failure; dropping the cache lets the next hover try again
    applications.rows.catch(() => { applications = null; });
  }
  return applications.rows;
}

function place(panel, anchor) {
  const box = anchor.getBoundingClientRect();
  const left = Math.max(8, Math.min(box.left, window.innerWidth - panel.offsetWidth - 8));
  let top = box.bottom + GAP;
  if (top + panel.offsetHeight > window.innerHeight - 8) top = Math.max(8, box.top - panel.offsetHeight - GAP);
  panel.style.left = `${left}px`;
  panel.style.top = `${top}px`;
}

function open(anchor) {
  closeCompanyCard();
  if (!anchor.isConnected) return;
  const company = anchor.dataset.company;
  const count = h("p", { class: "company-card-count", text: "Counting open postings…" });
  const held = h("div", { class: "company-card-apps" });
  const panel = h("div", { class: "company-card", role: "dialog", id: "company-card", "aria-label": company },
    h("div", { class: "company-card-head" }, avatar({ company, logo_domain: anchor.dataset.logo || null }, 48),
      h("div", { class: "company-card-name" }, h("p", { class: "company-card-title", text: company }), count)),
    held,
    h("button", { type: "button", class: "link-btn company-card-all", text: `See all at ${company}`,
      onclick: () => seeAllAt(company) }));
  panel.addEventListener("pointerenter", () => clearTimeout(closeTimer));
  panel.addEventListener("pointerleave", closeSoon);
  panel.addEventListener("focusout", (event) => {
    if (!panel.contains(/** @type {Node} */ (event.relatedTarget)) && event.relatedTarget !== anchor) closeSoon();
  });
  // opened from focus, the card follows the anchor's container in the DOM so Tab reaches it
  if (document.activeElement === anchor && anchor.parentElement) anchor.parentElement.after(panel);
  else document.body.append(panel);
  anchor.setAttribute("aria-describedby", "company-card");
  current = { panel, anchor };
  place(panel, anchor);
  fill(company, count, held, panel);
}

async function fill(company, count, held, panel) {
  const [jobs, rows] = await Promise.all([
    api(`/api/jobs${qs({ q: company, relevant_only: false, limit: 1 })}`, { quiet: true }).catch(() => null),
    loadApplications().catch(() => null)]);
  if (current?.panel !== panel) return;
  count.textContent = jobs ? fmt.plural(jobs.total, "open posting") : "Couldn't count the postings";
  const key = company.toLowerCase();
  const mine = (rows || []).filter((row) => String(row.company || "").toLowerCase() === key);
  if (!rows) held.replaceChildren(h("p", { class: "muted", text: "Couldn't load your applications" }));
  else if (!mine.length) held.replaceChildren(h("p", { class: "muted", text: "No applications at this company yet" }));
  else {
    const more = mine.length - SHOWN_APPLICATIONS;
    held.replaceChildren(h("p", { class: "company-card-label", text: "Your applications" }),
      h("ul", {}, mine.slice(0, SHOWN_APPLICATIONS).map((row) => h("li", {}, statusPill(row.status),
        h("span", { class: "company-card-role", text: row.title, title: row.title })))),
      ...(more > 0 ? [h("p", { class: "muted", text: `and ${more} more` })] : []));
  }
  place(panel, current.anchor);
}

/** Close the card and show every posting at `company`. @param {string} company */
export function seeAllAt(company) {
  closeCompanyCard();
  seeAll(company);
}

/**
 * Show a company card for any element with `data-company` (and `data-logo` for its
 * icon) after 400 ms of hover or focus. Call once at boot.
 * @param {{seeAll: (company: string) => void}} options  runs the card's "See all at" link
 */
export function initCompanyCards(options) {
  seeAll = options.seeAll;
  document.addEventListener("pointerover", (event) => {
    const anchor = anchorOf(event.target);
    if (anchor) openSoon(anchor);
  });
  document.addEventListener("pointerout", (event) => {
    const anchor = anchorOf(event.target);
    const to = /** @type {Node} */ (event.relatedTarget);
    if (!anchor || anchor.contains(to) || current?.panel.contains(to)) return;
    if (anchor === dismissed) dismissed = null;
    clearTimeout(openTimer);
    if (current?.anchor === anchor && document.activeElement !== anchor) closeSoon();
  });
  document.addEventListener("focusin", (event) => {
    const anchor = anchorOf(event.target);
    if (anchor && anchor === event.target) openSoon(anchor);
  });
  document.addEventListener("focusout", (event) => {
    const anchor = anchorOf(event.target);
    if (!anchor || current?.panel.contains(/** @type {Node} */ (event.relatedTarget))) return;
    if (anchor === dismissed) dismissed = null;
    closeSoon();
  });
  // capture runs before the shortcut registry, which skips a key whose default is prevented
  window.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    clearTimeout(openTimer);
    if (!current) return;
    const { panel, anchor } = current;
    const inCard = panel.contains(document.activeElement);
    dismissed = anchor;
    closeCompanyCard();
    event.preventDefault();
    if (inCard && anchor.isConnected) anchor.focus({ preventScroll: true });
  }, true);
  window.addEventListener("scroll", closeCompanyCard, true);
  window.addEventListener("resize", closeCompanyCard);
}
