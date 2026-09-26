import { h } from "../lib/dom.js";
import { addOverlay, keyLabel } from "../lib/keys.js";
import { setPref } from "../lib/store.js";

const PHONE = matchMedia("(max-width: 600px)");
const GAP = 12;
const WAIT_MS = 3000;

/**
 * @typedef {object} Step
 * @property {string} target  selector of the element the mark points at
 * @property {string} title
 * @property {string} text    one sentence
 * @property {() => void} [prepare]  bring the target on screen when it is missing
 */

/** @type {Step[]} */
const STEPS = [
  { target: ".jobs-view .filter-bar .switch", title: "Your matches",
    text: "This switch shows only postings that fit your preferences. Turn it off to see all postings." },
  { target: ".jobs-view .list .row .row-right", title: "Fit and tier",
    text: "Each row shows two scores out of 100, one for how well the role fits your profile and one for how strong the company is." },
  { target: "#detail .status-control", title: "Track it",
    text: "Set a posting's status here, from Saved to Offer. You can undo each change.",
    prepare: () => /** @type {HTMLElement | null} */ (document.querySelector(".jobs-view .list .row"))?.click() },
  { target: "#run-card", title: "Run now",
    text: "A run fetches new postings and ranks them. Start one here, or let the daily schedule do it." },
  { target: "#help-button", title: "Shortcuts",
    text: `Press ? for every shortcut, or ${keyLabel("mod+k")} to find any action by name.` },
];

let current = null;

/** @returns {boolean} whether the tour is showing */
export function tourOpen() {
  return current !== null;
}

addOverlay(tourOpen);

function visible(selector) {
  const el = document.querySelector(selector);
  if (!el) return null;
  const box = el.getBoundingClientRect();
  return box.width > 0 && box.height > 0 ? el : null;
}

function waitFor(selector, ms) {
  return new Promise((resolve) => {
    const end = Date.now() + ms;
    const poll = () => {
      const el = visible(selector);
      if (el || Date.now() > end) resolve(el);
      else setTimeout(poll, 100);
    };
    poll();
  });
}

/**
 * Put the card beside the target, below it when it fits, else above, else on
 * whichever side has room; clamped inside the window.
 */
function place(card, ring, target) {
  const centred = PHONE.matches || !target?.isConnected;
  card.classList.toggle("tour-centred", centred);
  ring.hidden = centred;
  if (centred) {
    card.style.left = "";
    card.style.top = "";
    return;
  }
  const box = target.getBoundingClientRect();
  Object.assign(ring.style, { left: `${box.left - 4}px`, top: `${box.top - 4}px`,
    width: `${box.width + 8}px`, height: `${box.height + 8}px` });
  const width = card.offsetWidth;
  const height = card.offsetHeight;
  const fitsBelow = box.bottom + GAP + height <= window.innerHeight - 8;
  const fitsAbove = box.top - GAP - height >= 8;
  let left;
  let top;
  if (fitsBelow || fitsAbove) {
    left = box.left;
    top = fitsBelow ? box.bottom + GAP : box.top - GAP - height;
  } else {
    left = box.right + GAP + width <= window.innerWidth - 8 ? box.right + GAP : box.left - GAP - width;
    top = box.top;
  }
  card.style.left = `${Math.max(8, Math.min(left, window.innerWidth - width - 8))}px`;
  card.style.top = `${Math.max(8, Math.min(top, window.innerHeight - height - 8))}px`;
}

/** End the tour and remember that it ran. @returns {boolean} whether it was showing */
export function endTour() {
  if (!current) return false;
  const { card, ring, cleanup } = current;
  current = null;
  cleanup();
  card.remove();
  ring.remove();
  setPref("toured", true);
  return true;
}

/**
 * Walk through STEPS with a coach mark on each target; a target that does not
 * show up gets a centred card. Skip ends it, as do Esc and a view change through
 * app.js. A target that leaves the page is looked up again, and the tour ends
 * when its selector matches nothing.
 */
export function startTour() {
  endTour();
  const ring = h("div", { class: "tour-ring", "aria-hidden": "true" });
  const count = h("p", { class: "tour-count section-label" });
  const title = h("h2", { class: "tour-title display", id: "tour-title" });
  const text = h("p", { class: "tour-text" });
  const next = h("button", { type: "button", class: "btn btn-primary btn-sm" });
  const skip = h("button", { type: "button", class: "link-btn", text: "Skip tour", title: "End the tour (Esc)",
    onclick: endTour });
  const card = h("div", { class: "tour-card", role: "dialog", "aria-modal": "false", "aria-labelledby": "tour-title" },
    count, title, text, h("div", { class: "tour-actions" }, skip, next));
  let index = 0;
  let target = null;
  let settled = false;
  const resized = new ResizeObserver(() => reflow());
  const aim = (el) => {
    resized.disconnect();
    target = el;
    if (el) resized.observe(el);
  };
  const reflow = () => current && place(card, ring, target);
  const detached = new MutationObserver(() => {
    if (!settled || !target || target.isConnected) return;
    const again = visible(STEPS[index].target);
    if (!again) {
      endTour();
      return;
    }
    aim(again);
    reflow();
  });
  const show = async (i) => {
    index = i;
    settled = false;
    const step = STEPS[i];
    aim(visible(step.target));
    if (!target && step.prepare && !PHONE.matches) {
      step.prepare();
      aim(await waitFor(step.target, WAIT_MS));
    }
    if (!current || index !== i) return;
    settled = true;
    count.textContent = `Step ${i + 1} of ${STEPS.length}`;
    title.textContent = step.title;
    text.textContent = step.text;
    const last = i === STEPS.length - 1;
    next.textContent = last ? "Done" : "Next";
    skip.hidden = last;
    next.title = last ? "End the tour" : "Show the next step";
    target?.scrollIntoView({ block: "nearest" });
    place(card, ring, target);
    next.focus({ preventScroll: true });
  };
  next.addEventListener("click", () => (index === STEPS.length - 1 ? endTour() : show(index + 1)));
  window.addEventListener("resize", reflow);
  document.addEventListener("scroll", reflow, true);
  detached.observe(document.body, { childList: true, subtree: true });
  current = { card, ring, cleanup: () => {
    resized.disconnect();
    detached.disconnect();
    window.removeEventListener("resize", reflow);
    document.removeEventListener("scroll", reflow, true);
  } };
  document.body.append(ring, card);
  show(0);
}

/** Start the tour once its first target is on screen; give up after a few seconds. */
export async function startTourWhenReady() {
  if (await waitFor(STEPS[0].target, WAIT_MS)) startTour();
}
