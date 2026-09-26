import { popoverOpen } from "../components/popover.js";
import { toast } from "../components/toast.js";
import { isTyping } from "./dom.js";

/**
 * @typedef {object} Shortcut
 * @property {string[]} keys  "j", "Enter", "Escape", "mod+k"; matched against event.key
 * @property {string} label   what the key does, for the help panel
 * @property {string} [group] help panel section
 * @property {boolean} [whileTyping] also fires while focus is in a text field or a popover is open
 * @property {boolean} [overOverlays] also fires while the palette or the tour is open
 * @property {boolean} [needsSelection] run from the palette with nothing selected, it says so in a toast
 * @property {boolean} [hidden] left out of the help panel and the command palette
 * @property {boolean} [noPalette] left out of the command palette only
 * @property {(event: KeyboardEvent) => boolean | void} run  returning false passes the key on
 */

/** @type {Shortcut[]} */
const registry = [];
/** @type {Set<() => boolean>} */
const overlays = new Set();

/**
 * Hold back every shortcut not marked `overOverlays` while `isOpen` returns true;
 * the palette and the tour call this once when their module loads.
 * @param {() => boolean} isOpen
 */
export function addOverlay(isOpen) {
  overlays.add(isOpen);
}

function overlayOpen() {
  return [...overlays].some((isOpen) => isOpen());
}

/**
 * Register shortcuts. Later registrations take precedence, so a mounted view's keys
 * win over the app's. Returns the function that removes them.
 * @param {Shortcut[]} shortcuts @returns {() => void}
 */
export function register(shortcuts) {
  registry.push(...shortcuts);
  return () => {
    for (const shortcut of shortcuts) {
      const index = registry.indexOf(shortcut);
      if (index >= 0) registry.splice(index, 1);
    }
  };
}

function matches(key, event) {
  if (key.startsWith("mod+")) {
    return (event.metaKey || event.ctrlKey) && event.key.toLowerCase() === key.slice(4);
  }
  if (event.metaKey || event.ctrlKey || event.altKey) return false;
  return event.key === key;
}

function onKeydown(event) {
  if (event.defaultPrevented || event.isComposing) return;
  const typing = isTyping(event.target) || popoverOpen();
  const covered = overlayOpen();
  for (let i = registry.length - 1; i >= 0; i -= 1) {
    const shortcut = registry[i];
    if (covered && !shortcut.overOverlays) continue;
    if (typing && !shortcut.whileTyping) continue;
    if (!shortcut.keys.some((key) => matches(key, event))) continue;
    if (shortcut.run(event) === false) continue;
    event.preventDefault();
    return;
  }
}

/** Listen for shortcuts on the document; call once at boot. */
export function install() {
  document.addEventListener("keydown", onKeydown);
}

/**
 * The registered, visible shortcuts grouped for the help panel and the command
 * palette, one entry per label; the newest registration of a label wins, as it
 * does for a key press. `run` performs the shortcut as if its first key were pressed.
 * @returns {Map<string, {keys: string[], label: string, noPalette: boolean, run: () => void}[]>}
 */
export function describe() {
  const groups = new Map();
  const seen = new Set();
  const newest = [...registry].reverse();
  for (const shortcut of registry) {
    if (shortcut.hidden || seen.has(shortcut.label)) continue;
    seen.add(shortcut.label);
    const winner = newest.find((known) => !known.hidden && known.label === shortcut.label);
    const group = shortcut.group || "General";
    if (!groups.has(group)) groups.set(group, []);
    groups.get(group).push({ keys: shortcut.keys, label: shortcut.label, noPalette: Boolean(winner.noPalette),
      run: () => {
        if (perform(winner) === false && winner.needsSelection) toast("Select a posting first");
      } });
  }
  return groups;
}

function perform(shortcut) {
  const key = shortcut.keys[0];
  return shortcut.run(/** @type {KeyboardEvent} */ (/** @type {unknown} */ ({
    key: key.startsWith("mod+") ? key.slice(4) : key, target: document.body,
    metaKey: false, ctrlKey: false, altKey: false, shiftKey: /^[A-Z]$/.test(key), preventDefault() {},
  })));
}

const KEY_NAMES = { Escape: "Esc" };
export const MAC = /Mac|iPhone|iPad/.test(navigator.platform);

/** A key as the help panel and tooltips show it. @param {string} key @returns {string} */
export function keyLabel(key) {
  if (/^[A-Z]$/.test(key)) return `${MAC ? "⇧" : "Shift+"}${key}`;
  if (!key.startsWith("mod+")) return KEY_NAMES[key] || key;
  return `${MAC ? "⌘" : "Ctrl+"}${key.slice(4).toUpperCase()}`;
}
