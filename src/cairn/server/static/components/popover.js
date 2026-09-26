import { h, rove } from "../lib/dom.js";

const GAP = 6;
let current = null;

/** @returns {boolean} whether a popover is open */
export function popoverOpen() {
  return current !== null;
}

/** Close the open popover, if any. @returns {boolean} whether one was open */
export function closePopover() {
  if (!current) return false;
  const { panel, anchor, onClose, cleanup } = current;
  current = null;
  cleanup();
  panel.remove();
  anchor?.setAttribute("aria-expanded", "false");
  onClose?.();
  if (panel.contains(document.activeElement) || document.activeElement === document.body) {
    anchor?.focus({ preventScroll: true });
  }
  return true;
}

/**
 * Put a fixed panel under `anchor`, or above it when there is no room below.
 * @param {HTMLElement} panel @param {HTMLElement} anchor @param {"start" | "end"} align
 */
export function place(panel, anchor, align) {
  const box = anchor.getBoundingClientRect();
  const width = panel.offsetWidth;
  const height = panel.offsetHeight;
  let left = align === "end" ? box.right - width : box.left;
  left = Math.max(8, Math.min(left, window.innerWidth - width - 8));
  let top = box.bottom + GAP;
  if (top + height > window.innerHeight - 8) top = Math.max(8, box.top - height - GAP);
  panel.style.left = `${left}px`;
  panel.style.top = `${top}px`;
}

/**
 * Open `content` in a panel under `anchor`, replacing any open popover. Outside
 * clicks and Esc close it; focus moves to its first control.
 * @param {HTMLElement} anchor
 * @param {HTMLElement | HTMLElement[]} content
 * @param {{label?: string, align?: "start" | "end", onClose?: () => void, className?: string}} [options]
 * @returns {HTMLElement} the panel
 */
export function openPopover(anchor, content, { label, align = "start", onClose, className = "" } = {}) {
  closePopover();
  const panel = h("div", { class: `popover ${className}`, role: "dialog",
    "aria-label": label || null }, content);
  document.body.append(panel);
  place(panel, anchor, align);
  anchor.setAttribute("aria-expanded", "true");
  const outside = (event) => {
    if (!panel.contains(event.target) && !anchor.contains(event.target)) closePopover();
  };
  const reposition = () => current?.panel === panel && place(panel, anchor, align);
  const detached = new MutationObserver(() => {
    if (!anchor.isConnected && current?.panel === panel) closePopover();
  });
  // registered after this click finishes, or the opening click would close it
  setTimeout(() => document.addEventListener("pointerdown", outside), 0);
  window.addEventListener("resize", reposition);
  detached.observe(document.body, { childList: true, subtree: true });
  current = { panel, anchor, onClose, cleanup: () => {
    document.removeEventListener("pointerdown", outside);
    window.removeEventListener("resize", reposition);
    detached.disconnect();
  } };
  panel.querySelector("input, button, select, textarea, [tabindex]")?.focus({ preventScroll: true });
  return panel;
}

/**
 * @param {HTMLElement} anchor @param {() => HTMLElement | HTMLElement[]} build
 * @param {Parameters<typeof openPopover>[2]} [options]
 */
export function togglePopover(anchor, build, options) {
  if (current?.anchor === anchor) {
    closePopover();
    return;
  }
  openPopover(anchor, build(), options);
}

/**
 * A vertical menu of actions for a popover; the arrow keys, Home and End move
 * between its items.
 * @param {{label: string, run: () => void, tone?: string}[]} items
 * @returns {HTMLElement}
 */
export function menu(items) {
  const buttons = items.map(({ label, run, tone }, i) => h("button", { type: "button", role: "menuitem",
    class: `menu-item ${tone ? `tone-${tone}` : ""}`, tabindex: i === 0 ? "0" : "-1", text: label,
    onclick: () => {
      closePopover();
      run();
    } }));
  return h("div", { class: "menu", role: "menu", onkeydown: (event) => rove(event, buttons) }, buttons);
}
