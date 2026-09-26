import { toast } from "../components/toast.js";

const KINDS = ["run_start", "phase_start", "phase_end", "posting_ranked", "info", "warn",
  "error", "run_done", "run_failed", "summary_start", "summary_done", "rank_progress", "summary_progress",
  "application_changed",
  "icons_progress", "icons_done", "icons_found", "feedback_changed", "links_done",
  "tracking_changed", "scores_explained"];
const RETRY_MS = 3000;

const handlers = new Map();
let source = null;
let retry = null;
let lost = false;
let warnedUnreadable = false;

function dispatch(kind, data) {
  for (const fn of handlers.get(kind) || []) fn(data);
}

function onMessage(kind, event) {
  let data;
  try {
    data = JSON.parse(event.data);
  } catch {
    if (!warnedUnreadable) toast("Cairn sent an update it couldn't read. Reload the page if things look stale", { tone: "error" });
    warnedUnreadable = true;
    return;
  }
  dispatch(kind, data);
}

function onConnectionError(event) {
  // the server's own `error` events arrive under the same name as MessageEvents
  if (event instanceof MessageEvent) return;
  if (!lost) toast("Lost touch with Cairn. Reconnecting…", { tone: "error" });
  lost = true;
  if (source?.readyState === EventSource.CLOSED) {
    source = null;
    retry = setTimeout(open, RETRY_MS);
  }
}

function onOpen() {
  if (!lost) return;
  lost = false;
  dispatch("reconnected", {});
}

function open() {
  retry = null;
  source = new EventSource("/api/run/events");
  source.addEventListener("open", onOpen);
  source.addEventListener("error", onConnectionError);
  for (const kind of KINDS) {
    source.addEventListener(kind, (event) => event instanceof MessageEvent && onMessage(kind, event));
  }
}

/** Open the page's one EventSource; later calls do nothing. */
export function connect() {
  if (source || retry) return;
  open();
}

/**
 * Call fn(data) for every `name` event. Besides the server's events, `reconnected`
 * fires when the stream comes back after a loss, when events may have been missed.
 * @param {string} name @param {(data: any) => void} fn @returns {() => void}
 */
export function subscribe(name, fn) {
  if (!handlers.has(name)) handlers.set(name, new Set());
  handlers.get(name).add(fn);
  return () => handlers.get(name).delete(fn);
}

function disconnect() {
  clearTimeout(retry);
  retry = null;
  source?.close();
  source = null;
}

// a page parked in the back/forward cache kept its stream open, and five of them
// used up the browser's six connections to the server, stalling every request
window.addEventListener("pagehide", disconnect);
window.addEventListener("pageshow", (event) => event.persisted && connect());
