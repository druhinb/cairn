const KEY = "cairn.ui";

function readAll() {
  try {
    const value = JSON.parse(localStorage.getItem(KEY) || "{}");
    return value && typeof value === "object" ? value : {};
  } catch {
    // unreadable or blocked storage starts from the default preferences
    return {};
  }
}

/**
 * One persisted UI preference from the `cairn.ui` localStorage entry.
 * @template T @param {string} name @param {T} fallback @returns {T}
 */
export function pref(name, fallback) {
  const value = readAll()[name];
  return value === undefined ? fallback : value;
}

/** @param {string} name @param {any} value */
export function setPref(name, value) {
  const all = readAll();
  all[name] = value;
  try {
    localStorage.setItem(KEY, JSON.stringify(all));
  } catch {
    // a full or disabled localStorage keeps the preference for this page load only
  }
}

const OPENED_LIMIT = 500;
let opened = null;
let writeQueued = false;

function openedList() {
  opened ??= pref("opened", []).slice(-OPENED_LIMIT);
  return opened;
}

function writeOpened() {
  writeQueued = false;
  if (opened) setPref("opened", opened);
}

/** Posting ids opened in this browser, capped at the newest OPENED_LIMIT. */
export function openedIds() {
  return new Set(openedList());
}

/** Record an opened posting; the write to localStorage waits for the next frame. @param {string} id */
export function markOpened(id) {
  opened = [...openedList().filter((known) => known !== id), id].slice(-OPENED_LIMIT);
  if (writeQueued) return;
  writeQueued = true;
  requestAnimationFrame(writeOpened);
}

// hidden tabs get no animation frames, so a tab closed in the background writes here
window.addEventListener("pagehide", () => writeQueued && writeOpened());
