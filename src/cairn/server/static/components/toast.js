import { h } from "../lib/dom.js";

const MAX = 3;
const LIFETIME_MS = 5000;

function root() {
  return document.getElementById("toasts");
}

/**
 * Show a toast bottom-left for five seconds; at most three stay on screen.
 * `undo` adds an Undo button that runs it and dismisses the toast.
 * @param {string} message
 * @param {{undo?: () => void, tone?: "error" | "info"}} [options]
 * @returns {() => void} dismiss
 */
export function toast(message, { undo, tone = "info" } = {}) {
  const host = root();
  if (!host) return () => {};
  let timer;
  const node = h("div", { class: `toast toast-${tone}`, role: tone === "error" ? "alert" : "status" },
    h("span", { class: "toast-text", text: message }),
    undo && h("button", { type: "button", class: "btn btn-ghost btn-sm", text: "Undo",
      onclick: () => {
        dismiss();
        undo();
      } }),
    h("button", { type: "button", class: "icon-btn toast-close", "aria-label": "Dismiss",
      text: "×", onclick: () => dismiss() }));
  const dismiss = () => {
    clearTimeout(timer);
    node.remove();
  };
  // a repeated message replaces its older copy, so one failure shows as one toast
  for (const old of [...host.children]) if (old.textContent === node.textContent) old.remove();
  host.append(node);
  while (host.children.length > MAX) host.firstElementChild.remove();
  timer = setTimeout(dismiss, LIFETIME_MS);
  node.addEventListener("mouseenter", () => clearTimeout(timer));
  node.addEventListener("mouseleave", () => {
    timer = setTimeout(dismiss, LIFETIME_MS);
  });
  return dismiss;
}
