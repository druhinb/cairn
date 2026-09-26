import { h } from "../lib/dom.js";
import { icon } from "./icons.js";

/**
 * A list edited as tags: Enter or comma adds, Backspace on an empty input removes
 * the last, pasted text splits on commas and newlines. `onChange` gets a new array.
 * @param {string[]} initial
 * @param {{id?: string, placeholder?: string, onChange?: (tags: string[]) => void}} [options]
 * @returns {HTMLElement}
 */
export function tagInput(initial, { id, placeholder = "Add…", onChange } = {}) {
  let tags = [...initial];
  const input = h("input", { id, type: "text", class: "tag-entry", placeholder });
  const box = h("div", { class: "tag-input", onclick: (e) => e.target === box && input.focus() }, input);
  const commit = (next) => {
    tags = next;
    draw();
    onChange?.([...tags]);
  };
  const add = (text) => {
    const fresh = text.split(/[,\n]/).map((part) => part.trim())
      .filter((part) => part && !tags.includes(part));
    if (fresh.length) commit([...tags, ...fresh]);
    input.value = "";
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === ",") {
      e.preventDefault();
      add(input.value);
    } else if (e.key === "Backspace" && !input.value && tags.length) {
      commit(tags.slice(0, -1));
    }
  });
  input.addEventListener("paste", (e) => {
    const text = e.clipboardData?.getData("text") || "";
    if (/[,\n]/.test(text)) {
      e.preventDefault();
      add(text);
    }
  });
  input.addEventListener("blur", () => input.value.trim() && add(input.value));
  // the entry stays in place, so typing one tag after another keeps focus
  function draw() {
    for (const chip of box.querySelectorAll(".tag")) chip.remove();
    input.before(...tags.map((tag, index) => h("span", { class: "chip chip-fill tag" }, tag,
      h("button", { type: "button", class: "tag-remove", "aria-label": `Remove ${tag}`,
        onclick: () => {
          commit(tags.filter((_, i) => i !== index));
          input.focus();
        } }, icon("x")))));
  }
  draw();
  return box;
}
