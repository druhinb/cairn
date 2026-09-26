import { safeUrl } from "../lib/dom.js";
import { toast } from "../components/toast.js";
import * as detail from "./detail.js";

const OPEN_NEXT = 5;

/**
 * Open a posting's link in a new tab.
 * @param {{id: string, url?: string} | null | undefined} job
 * @returns {boolean} whether the posting had a web link to open
 */
export function openPosting(job) {
  const url = safeUrl(job?.url);
  if (!url) {
    toast("This posting has no web link", { tone: "error" });
    return false;
  }
  window.open(url, "_blank", "noopener,noreferrer");
  detail.noteOpened(job.id);
  return true;
}

/**
 * The Open next queue: the top OPEN_NEXT postings with a link and not opened yet.
 * The first open fixes the set, so opening one does not pull the sixth posting in;
 * `reset` forgets it when the list behind it changes.
 */
export function openNextQueue() {
  let queue = null;
  const candidates = (order, opened) => (queue ?? order)
    .filter((job) => !opened.has(job.id) && safeUrl(job.url)).slice(0, OPEN_NEXT);
  return {
    /** @param {object[]} order @param {Set<string>} opened @returns {object[]} */
    candidates,
    /**
     * Open the next candidate. @param {object[]} order @param {Set<string>} opened
     * @returns {object | null} the posting opened
     */
    open(order, opened) {
      queue ??= candidates(order, opened);
      const [job] = candidates(order, opened);
      return job && openPosting(job) ? job : null;
    },
    reset() {
      queue = null;
    },
  };
}

/**
 * Change one posting's status through the detail pane, which offers Undo.
 * @param {object} job @param {string} status
 */
export async function setStatus(job, status) {
  try {
    await detail.changeStatus(job, status);
  } catch {
    // api() has shown the error
  }
}

/**
 * The keys every posting list shares: j/k move, Enter or o opens the posting, s, a
 * and x set a status, n focuses the note. `act` replaces the status change, as for
 * a bulk selection; it returns false when there is nothing to act on.
 * @param {{group: string, selected: () => object | null | undefined,
 *   move: (step: number) => unknown, act?: (status: string) => boolean}} list
 * @returns {import("../lib/keys.js").Shortcut[]}
 */
export function postingShortcuts({ group, selected, move, act }) {
  const withSelected = (fn) => () => {
    const job = selected();
    if (!job) return false;
    fn(job);
    return true;
  };
  const status = (next) => () => (act ? act(next) : withSelected((job) => setStatus(job, next))());
  return [
    { keys: ["j", "ArrowDown"], label: "Next posting", group, run: () => move(1) },
    { keys: ["k", "ArrowUp"], label: "Previous posting", group, run: () => move(-1) },
    { keys: ["Enter", "o"], label: "Open the posting", group, needsSelection: true,
      run: (event) => !event.target.closest?.("button, a, summary, select") && withSelected(openPosting)() },
    { keys: ["s"], label: "Save", group, needsSelection: true, run: status("saved") },
    { keys: ["a"], label: "Mark applied", group, needsSelection: true, run: status("applied") },
    { keys: ["x"], label: "Pass", group, needsSelection: true, run: status("passed") },
    { keys: ["n"], label: "Write a note", group, needsSelection: true, run: () => detail.focusNote() },
  ];
}
