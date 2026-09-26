import { h } from "../lib/dom.js";
import { addOverlay, describe, keyLabel } from "../lib/keys.js";
import { pref, setPref } from "../lib/store.js";
import { icon } from "./icons.js";

const RECENT_MAX = 8;

/**
 * @typedef {object} Command
 * @property {string} id       stable across renders; recent commands are stored by it
 * @property {string} label
 * @property {string} group    the view or area it belongs to
 * @property {string[]} [keys] its shortcut, shown beside it
 * @property {(query: string) => void} run
 * @property {boolean} [last]  listed after every match and never stored as recent
 */

/** @type {Set<(query: string) => Command[]>} */
const providers = new Set();
let current = null;

/**
 * Offer commands in the palette besides the registered shortcuts. The provider
 * runs on every keystroke with the typed query. Returns the function that removes it.
 * @param {(query: string) => Command[]} provider @returns {() => void}
 */
export function addCommands(provider) {
  providers.add(provider);
  return () => providers.delete(provider);
}

/** @returns {boolean} whether the palette is open */
export function paletteOpen() {
  return current !== null;
}

addOverlay(paletteOpen);

function allCommands(query) {
  const shortcuts = [...describe()].flatMap(([group, entries]) => entries
    .filter((entry) => !entry.noPalette)
    .map(({ keys, label, run }) => ({ id: `${group}:${label}`, label, group, keys, run: () => run() })));
  const seen = new Set();
  return [...shortcuts, ...[...providers].flatMap((provider) => provider(query))]
    .filter((command) => !seen.has(command.id) && seen.add(command.id));
}

/**
 * A subsequence match of `query` in `text`, scored higher for runs of adjacent
 * letters and for letters that start a word; null when a letter is missing. A
 * letter scores 1 alone, so a total of at most twice the query's length means
 * letters scattered through unrelated words.
 */
function score(text, query) {
  const hay = text.toLowerCase();
  let at = 0;
  let total = 0;
  let run = 0;
  for (const ch of query) {
    const found = hay.indexOf(ch, at);
    if (found < 0) return null;
    run = found === at && at > 0 ? run + 1 : 0;
    const wordStart = found === 0 || /[\s›:(-]/.test(hay[found - 1]);
    total += 1 + run * 2 + (wordStart ? 3 : 0);
    at = found + 1;
  }
  return total;
}

function recentIds() {
  const stored = pref("palette", []);
  return Array.isArray(stored) ? stored.filter((id) => typeof id === "string") : [];
}

function remember(id) {
  setPref("palette", [id, ...recentIds().filter((known) => known !== id)].slice(0, RECENT_MAX));
}

/** The commands to list for `query`, with the heading each starts, if any. */
function matches(query) {
  const needle = query.toLowerCase().replace(/\s+/g, "");
  const commands = allCommands(query.trim());
  const recent = recentIds();
  const rank = (command) => {
    const index = recent.indexOf(command.id);
    return index < 0 ? RECENT_MAX : index;
  };
  const [tail, body] = [commands.filter((c) => c.last), commands.filter((c) => !c.last)];
  if (!needle) {
    const known = body.filter((c) => rank(c) < RECENT_MAX).sort((a, b) => rank(a) - rank(b));
    const groups = [...new Set(body.map((c) => c.group))];
    const rest = body.filter((c) => rank(c) === RECENT_MAX)
      .sort((a, b) => groups.indexOf(a.group) - groups.indexOf(b.group));
    return [...known.map((c, i) => ({ command: c, heading: i === 0 ? "Recent" : null })),
      ...rest.map((c, i) => ({ command: c, heading: i === 0 || rest[i - 1].group !== c.group ? c.group : null }))];
  }
  const within = (command) => command.label.toLowerCase().replace(/\s+/g, "").includes(needle);
  const scored = body.map((command) => ({ command, score: score(`${command.label} ${command.group}`, needle) }))
    .filter((entry) => entry.score != null && (entry.score > needle.length * 2 || within(entry.command)))
    .sort((a, b) => b.score - a.score || rank(a.command) - rank(b.command));
  return [...scored, ...tail.map((command) => ({ command }))].map(({ command }) => ({ command, heading: null }));
}

/** Close the palette, if open. @returns {boolean} whether it was open */
export function closePalette() {
  if (!current) return false;
  const { backdrop, returnTo } = current;
  current = null;
  backdrop.remove();
  if (returnTo?.isConnected) returnTo.focus({ preventScroll: true });
  return true;
}

export function togglePalette() {
  if (!closePalette()) openPalette();
}

function openPalette() {
  const input = h("input", { type: "text", class: "palette-input", role: "combobox",
    "aria-expanded": "true", "aria-controls": "palette-list", "aria-autocomplete": "list",
    "aria-label": "Command", placeholder: "Type a command or a view", autocomplete: "off", spellcheck: "false" });
  const list = h("div", { id: "palette-list", class: "palette-list", role: "listbox", "aria-label": "Commands" });
  const panel = h("div", { class: "palette", role: "dialog", "aria-modal": "true", "aria-label": "Command palette" },
    h("div", { class: "palette-search" }, icon("search"), input, h("kbd", { text: "Esc" })),
    list,
    h("p", { class: "palette-foot" }, h("kbd", { text: "↑" }), h("kbd", { text: "↓" }), " move · ",
      h("kbd", { text: "Enter" }), " run · ", h("kbd", { text: "Esc" }), " close"));
  const backdrop = h("div", { class: "palette-backdrop", onpointerdown: (event) => {
    if (event.target === backdrop) closePalette();
  }, onmousedown: (event) => {
    if (event.target !== input) event.preventDefault();
  } }, panel);
  // the input is the palette's one focusable control, so focus never leaves it while open
  panel.addEventListener("focusout", (event) => {
    if (current === state && !panel.contains(/** @type {Node | null} */ (event.relatedTarget))) {
      setTimeout(() => current === state && input.focus({ preventScroll: true }), 0);
    }
  });
  const state = { backdrop, returnTo: /** @type {HTMLElement} */ (document.activeElement), entries: [], active: 0 };
  current = state;

  const setActive = (index) => {
    state.active = index;
    list.querySelectorAll(".palette-item").forEach((item, i) => {
      item.setAttribute("aria-selected", String(i === index));
      if (i === index) {
        input.setAttribute("aria-activedescendant", item.id);
        item.scrollIntoView({ block: "nearest" });
      }
    });
  };
  const run = (index) => {
    const entry = state.entries[index];
    if (!entry) return;
    const query = input.value.trim();
    closePalette();
    if (!entry.command.last) remember(entry.command.id);
    entry.command.run(query);
  };
  const render = () => {
    state.entries = matches(input.value);
    input.removeAttribute("aria-activedescendant");
    list.replaceChildren(...(state.entries.length ? state.entries.flatMap(({ command, heading }, i) => [
      heading && h("div", { class: "palette-heading section-label", role: "presentation", text: heading }),
      h("div", { class: "palette-item", role: "option", id: `palette-${i}`, "aria-selected": "false",
        onpointermove: () => state.active !== i && setActive(i),
        onclick: () => run(i) },
      h("span", { class: "palette-label", text: command.label }),
      input.value.trim() && !command.last && h("span", { class: "palette-group", text: command.group }),
      command.keys?.length > 0 && h("span", { class: "palette-keys" },
        command.keys.filter((key) => !key.startsWith("Arrow")).slice(0, 2)
          .map((key) => h("kbd", { text: keyLabel(key) }))))]).filter(Boolean)
      : [h("p", { class: "palette-empty", text: "No command matches." })]));
    if (state.entries.length) setActive(0);
  };
  input.addEventListener("input", render);
  input.addEventListener("keydown", (event) => {
    const step = { ArrowDown: 1, ArrowUp: -1 }[event.key];
    if (step && state.entries.length) {
      setActive((state.active + step + state.entries.length) % state.entries.length);
    } else if (event.key === "Enter") {
      run(state.active);
    } else if (event.key === "Escape") {
      closePalette();
    } else if (event.key !== "Tab") {
      return;
    }
    event.preventDefault();
  });
  document.body.append(backdrop);
  render();
  input.focus();
}
