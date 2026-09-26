import { h } from "../lib/dom.js";

/**
 * A Plex Mono text editor with a line-number gutter, `TODO:` lines highlighted and
 * counted. `onInput` gets the text and the TODO count after every edit.
 * @param {string} text
 * @param {{label: string, onInput?: (text: string, todos: number) => void}} options
 * @returns {{element: HTMLElement, value: () => string, setValue: (text: string) => void}}
 */
export function editor(text, { label, onInput }) {
  const gutter = h("div", { class: "editor-gutter", "aria-hidden": "true" });
  const marks = h("div", { class: "editor-marks", "aria-hidden": "true" });
  const area = h("textarea", { class: "editor-text", spellcheck: "false", "aria-label": label });
  const todos = h("span", { class: "editor-todos" });
  const draw = () => {
    const lines = area.value.split("\n");
    gutter.replaceChildren(...lines.map((_, i) => h("div", { text: String(i + 1) })));
    marks.replaceChildren(...lines.map((line) => h("div",
      { class: /^\s*(?:[-*]\s*)?TODO:/.test(line) ? "todo" : "" }, "​")));
    const count = marks.querySelectorAll(".todo").length;
    // an empty file has no TODOs because it has no answers either, so the foot says nothing
    todos.textContent = count ? `${count} TODO${count === 1 ? "" : "s"} left` : area.value.trim() ? "No TODOs left" : "";
    return count;
  };
  area.addEventListener("input", () => onInput?.(area.value, draw()));
  area.addEventListener("scroll", () => {
    gutter.scrollTop = area.scrollTop;
    marks.scrollTop = area.scrollTop;
  });
  area.value = text;
  draw();
  const element = h("div", { class: "editor" },
    h("div", { class: "editor-body" }, gutter, h("div", { class: "editor-field" }, marks, area)),
    h("div", { class: "editor-foot" }, todos));
  return { element, value: () => area.value, setValue: (next) => {
    area.value = next;
    draw();
  } };
}
