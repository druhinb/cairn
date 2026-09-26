import { h } from "../lib/dom.js";
import { copyText } from "../lib/clipboard.js";
import { icon } from "./icons.js";

const NAMES = { python: "Python", claude: "Claude Code", home: "Setup", schedule: "Daily schedule",
  pdftotext: "PDF reader", "llm key": "AI provider key", llm: "AI provider", "start at login": "Start at login" };

// doctor states a fix as "run: <command>", a bare brew command, or a sentence
function command(fix) {
  if (fix.startsWith("run: ")) return fix.slice(5);
  return fix.startsWith("brew ") ? fix : null;
}

/**
 * The doctor checks as rows: passed, failed, or an optional check that failed, each
 * with its detail and the command that fixes it.
 * @param {{name: string, ok: boolean, required: boolean, detail: string, fix: string}[]} checks
 * @returns {HTMLElement}
 */
export function doctorList(checks) {
  return h("ul", { class: "doctor" }, checks.map((check) => {
    const tone = check.ok ? "ok" : check.required ? "fail" : "optional";
    const mark = { ok: ["check", "Passed"], fail: ["x", "Failed"], optional: ["circle", "Optional, not set up"] }[tone];
    return h("li", { class: `doctor-row doctor-${tone}` },
      h("span", { class: "doctor-mark" }, icon(mark[0], mark[1])),
      h("div", { class: "doctor-text" },
        h("span", { class: "doctor-name", text: NAMES[check.name] || check.name }),
        h("span", { class: "doctor-detail", text: check.detail })),
      check.fix && fixLine(check.fix));
  }));
}

function fixLine(fix) {
  const cmd = command(fix);
  if (!cmd) return h("p", { class: "doctor-hint", text: fix[0].toUpperCase() + fix.slice(1) });
  return h("div", { class: "doctor-fix" },
    h("code", { class: "code-chip", text: cmd }),
    h("button", { type: "button", class: "icon-btn", title: "Copy the command",
      "aria-label": `Copy ${cmd}`, onclick: () => copyText(cmd, "the command") }, icon("copy")));
}
