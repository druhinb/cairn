import { h } from "../lib/dom.js";
import { switchControl } from "./switch.js";

const STAMP = /^(\d{4}-\d\d-\d\d) (\d\d:\d\d:\d\d) {2}(.*)$/;
const BOTTOM_SLACK = 8;

function level(text) {
  if (text.startsWith("WARN ")) return "warn";
  if (text.startsWith("ERROR ") || /^run \d+ failed/.test(text)) return "error";
  if (text.startsWith("== ")) return "phase";
  return "info";
}

const pad = (n) => String(n).padStart(2, "0");

// mirrors cairn/core/logfile.py, so a live run reads like its stored log
const LINES = {
  run_start: (d) => `run ${d.run_id} started  limit=${d.limit ?? "None"}  fit=${d.fit ?? "None"}  dry_run=${d.dry_run ? "True" : "False"}`,
  phase_start: (d) => `== ${d.name} ==`,
  phase_end: (d) => `[${d.name}] ${Number(d.seconds).toFixed(1)}s`,
  info: (d) => d.text,
  warn: (d) => `WARN ${d.text}`,
  error: (d) => `ERROR ${d.text}`,
  rank_progress: (d) => `[rank] ranked ${d.done} of ${d.total}`,
  summary_progress: (d) => `[jd] summarised ${d.done} of ${d.total}`,
  posting_ranked: (d) => `[rank] fit ${d.fit} tier ${d.tier}${d.below_floor ? "  below floor" : ""}  ${d.company} — ${d.title}  ${d.id}`,
  run_done: (d) => [`run ${d.run_id} ${d.status} in ${Number(d.elapsed).toFixed(1)}s`,
    ...Object.entries(d.counts || {}).map(([k, v]) => `${k}=${v}`)].join("  "),
  run_failed: (d) => `run ${d.run_id} failed: ${d.error}`,
};

/**
 * A run event as the log line cairn writes for it, or null for kinds the run
 * log leaves to other views.
 * @param {string} kind @param {{at: number} & Record<string, any>} data @returns {string | null}
 */
export function eventLine(kind, data) {
  const render = LINES[kind];
  if (!render) return null;
  const at = new Date(data.at * 1000);
  const day = `${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())}`;
  const time = `${pad(at.getHours())}:${pad(at.getMinutes())}:${pad(at.getSeconds())}`;
  return `${day} ${time}  ${String(render(data)).split("\n").join(" ")}`;
}

/**
 * A scrolling log in Plex Mono: warn and error lines coloured, a filter, and a
 * Follow switch that keeps the newest line in view. Scrolling up turns Follow off.
 * @param {{label: string, lines?: string[], empty?: string}} options
 * @returns {{element: HTMLElement, append: (lines: string[]) => void, setLines: (lines: string[]) => void}}
 */
export function logConsole({ label, lines = [], empty = "No log lines yet." }) {
  let all = [];
  let query = "";
  let follow = true;
  const body = h("div", { class: "log-body", role: "log", "aria-label": label, tabindex: "0" });
  const count = h("span", { class: "log-count mono" });
  const filter = h("input", { type: "search", class: "input log-filter", placeholder: "Filter the log",
    "aria-label": `Filter ${label.toLowerCase()}` });
  const followSwitch = switchControl("Follow", follow, (on) => {
    follow = on;
    if (on) body.scrollTop = body.scrollHeight;
  }, { key: "follow" });
  const followInput = /** @type {HTMLInputElement} */ (followSwitch.querySelector("input"));

  const lineEl = (raw) => {
    const match = raw.match(STAMP);
    const text = match ? match[3] : raw;
    return h("div", { class: `log-line log-${level(text)}` },
      match && h("span", { class: "log-time", title: `${match[1]} ${match[2]}`, text: match[2] }),
      h("span", { class: "log-text", text }));
  };
  const shown = (raw) => !query || raw.toLowerCase().includes(query);
  const renderCount = () => {
    const visible = query ? all.filter(shown).length : all.length;
    count.textContent = query ? `${visible} of ${all.length} lines` : `${all.length} lines`;
  };
  const draw = () => {
    const visible = all.filter(shown);
    body.replaceChildren(...(visible.length ? visible.map(lineEl)
      : [h("p", { class: "log-empty", text: all.length ? "No lines match the filter." : empty })]));
    renderCount();
    if (follow) body.scrollTop = body.scrollHeight;
  };

  filter.addEventListener("input", () => {
    query = filter.value.trim().toLowerCase();
    draw();
  });
  body.addEventListener("scroll", () => {
    const atBottom = body.scrollHeight - body.scrollTop - body.clientHeight <= BOTTOM_SLACK;
    if (follow && !atBottom) {
      follow = false;
      followInput.checked = false;
    }
  });

  const element = h("div", { class: "log-console" },
    h("div", { class: "log-bar" }, filter, count, followSwitch), body);
  const setLines = (next) => {
    all = next.filter((line) => line !== "");
    draw();
  };
  setLines(lines);
  return {
    element,
    setLines,
    append(next) {
      const fresh = next.filter((line) => line !== "");
      if (!fresh.length) return;
      const wasEmpty = all.length === 0;
      all.push(...fresh);
      if (wasEmpty || query) {
        draw();
        return;
      }
      body.append(...fresh.map(lineEl));
      renderCount();
      if (follow) body.scrollTop = body.scrollHeight;
    },
  };
}
