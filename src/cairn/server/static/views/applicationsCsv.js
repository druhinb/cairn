import { api, qs } from "../lib/api.js";
import { copyText } from "../lib/clipboard.js";
import { formatCsv, parseCsv } from "../lib/csv.js";
import { fmt, h, safeUrl } from "../lib/dom.js";
import { STATUS_LABELS, statusPill } from "../components/pill.js";
import { toast } from "../components/toast.js";

const EXPORT_COLUMNS = ["id", "company", "title", "status", "applied_at", "updated_at", "note", "url", "fit",
  "tier", "source"];
const HEADER_ALIASES = { link: "url", "job url": "url", "company name": "company", "posting id": "id",
  role: "title", position: "title" };
const PREVIEW_ROWS = 5;
const LOOKUP_PAGE = 200;
const LOOKUP_MAX = 1000;
const IMPORT_MAX = 2000;
const WORKERS = 4;
const DEFAULT_STATUS = "saved";

/** Download every application as a CSV file. */
export async function exportCsv() {
  let body;
  try {
    body = await api("/api/applications");
  } catch {
    // api() has shown the error
    return;
  }
  const text = formatCsv([EXPORT_COLUMNS, ...body.rows.map((row) => EXPORT_COLUMNS.map((key) => row[key]))]);
  const href = URL.createObjectURL(new Blob([text], { type: "text/csv" }));
  const link = h("a", { href, download: `applications-${new Date().toISOString().slice(0, 10)}.csv`, hidden: true });
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(href), 1000);
  toast(`Exported ${fmt.plural(body.rows.length, "application")}`);
}

const sameUrl = (a, b) => Boolean(a && b) && a.replace(/\/+$/, "") === b.replace(/\/+$/, "");

/**
 * The file's data rows, up to IMPORT_MAX, as records keyed by header name, each
 * with the application it matches if already tracked. `extra` counts the rows
 * past the cap.
 * @returns {{records: object[], extra: number, error: string | null}}
 */
function readRecords(text, tracked) {
  let rows;
  try {
    rows = parseCsv(text);
  } catch (error) {
    return { records: [], extra: 0, error: `The file is not valid CSV: ${error.message}.` };
  }
  if (rows.length < 2) {
    return { records: [], extra: 0, error: "The file needs a header row and at least one row below it." };
  }
  const header = rows[0].map((name) => {
    const key = name.trim().toLowerCase();
    return HEADER_ALIASES[key] ?? key;
  });
  if (!header.includes("url") && !header.includes("id")) {
    return { records: [], extra: 0, error: "The file needs a url or an id column." };
  }
  const records = rows.slice(1, IMPORT_MAX + 1).map((fields, i) => {
    const raw = (name) => (header.includes(name) ? fields[header.indexOf(name)] ?? "" : null);
    const get = (name) => (raw(name) ?? "").trim();
    const status = get("status").toLowerCase() || null;
    const record = { line: i + 2, id: get("id"), url: get("url"), company: get("company"), title: get("title"),
      status, note: raw("note") };
    record.tracked = (record.id && tracked.find((row) => row.id === record.id))
      || tracked.find((row) => sameUrl(row.url, record.url)) || null;
    if (status && !STATUS_LABELS[status]) record.problem = `unknown status “${status}”`;
    else if (!record.id && !record.url) record.problem = "no url or id";
    return record;
  });
  return { records, extra: Math.max(0, rows.length - 1 - IMPORT_MAX), error: null };
}

/** Up to LOOKUP_MAX postings that a search for `company` finds, closed and passed ones included. */
async function postingsAt(company) {
  const rows = [];
  while (rows.length < LOOKUP_MAX) {
    const page = await api(`/api/jobs${qs({ relevant_only: false, hide_passed: false, active_only: false,
      q: company, limit: LOOKUP_PAGE, offset: rows.length })}`, { quiet: true });
    rows.push(...page.rows);
    if (page.rows.length < LOOKUP_PAGE || rows.length >= page.total) break;
  }
  return rows;
}

/**
 * The posting a record names: its tracked application, else the posting with its
 * id, else the posting at its company with its url; null when none matches.
 * Rejects when a lookup fails.
 */
async function resolve(record, companies) {
  if (record.tracked) return record.tracked;
  if (record.id) {
    try {
      return await api(`/api/jobs/${encodeURIComponent(record.id)}`, { quiet: true });
    } catch (error) {
      if (error.status !== 404) throw error;
    }
  }
  if (!record.url || !record.company) return null;
  const key = record.company.toLowerCase();
  if (!companies.has(key)) companies.set(key, postingsAt(record.company));
  return (await companies.get(key)).find((row) => sameUrl(row.url, record.url)) ?? null;
}

/**
 * The PUT body that brings `job` in line with the record, or null when nothing
 * would change. A record without a status keeps a posting's status, and gives
 * one that has none the status Saved.
 */
function change(record, job) {
  const status = record.status ?? (job.status ? null : DEFAULT_STATUS);
  const noteChanges = record.note != null && record.note !== (job.note || "");
  if (status == null || status === job.status) return noteChanges ? { note: record.note } : null;
  return record.note == null ? { status } : { status, note: record.note };
}

/**
 * Apply the records WORKERS at a time. Setting `stop.requested` ends the import
 * once the requests in flight return.
 */
async function importRecords(records, stop, onProgress) {
  const companies = new Map();
  const outcome = { imported: 0, unchanged: 0, unmatched: [], unchecked: [], unsaved: [], notTried: 0 };
  let next = 0;
  let finished = 0;
  const one = async (record) => {
    let job;
    try {
      job = await resolve(record, companies);
    } catch (error) {
      outcome.unchecked.push({ record, reason: error.message });
      return;
    }
    if (!job) {
      outcome.unmatched.push(record);
      return;
    }
    const body = change(record, job);
    if (!body) {
      outcome.unchanged += 1;
      return;
    }
    try {
      await api(`/api/jobs/${encodeURIComponent(job.id)}/application`, { method: "PUT", quiet: true, body });
      outcome.imported += 1;
    } catch (error) {
      outcome.unsaved.push({ record, reason: error.message });
    }
  };
  const work = async () => {
    while (next < records.length && !stop.requested) {
      const record = records[next];
      next += 1;
      await one(record);
      finished += 1;
      onProgress(finished);
    }
  };
  await Promise.all(Array.from({ length: WORKERS }, work));
  outcome.notTried = records.length - finished;
  return outcome;
}

function describeRecord(record) {
  return [record.company, record.title, record.url || record.id].filter(Boolean).join(", ");
}

/**
 * The import card: a preview of the chosen file, then the result. `onClose` runs
 * when the user dismisses it.
 * @param {File} file @param {() => void} onClose @returns {Promise<HTMLElement>}
 */
export async function importCard(file, onClose) {
  const card = h("section", { class: "import-card", "aria-label": "Import applications" });
  const stop = { requested: false, running: false };
  const close = h("button", { type: "button", class: "btn btn-sm btn-ghost", text: "Cancel",
    title: "Close without importing", onclick: () => {
      if (!stop.running) {
        onClose();
        return;
      }
      stop.requested = true;
      close.disabled = true;
      close.textContent = "Stopping…";
    } });
  const head = (title, ...rest) => h("header", { class: "import-head" },
    h("h2", { class: "import-title", text: title }), h("span", { class: "import-file mono", text: file.name }), ...rest);
  let text;
  let tracked;
  try {
    [text, tracked] = await Promise.all([file.text(), api("/api/applications").then((body) => body.rows)]);
  } catch (error) {
    card.replaceChildren(head("Import failed"), h("p", { class: "import-error", text: `Couldn't read the file: ${error.message}` }),
      h("div", { class: "import-actions" }, close));
    return card;
  }
  const { records, extra, error } = readRecords(text, tracked);
  if (error) {
    card.replaceChildren(head("Can't import this file"), h("p", { class: "import-error", text: error }),
      h("p", { class: "muted", text: "Export CSV writes a file in the expected shape." }),
      h("div", { class: "import-actions" }, close));
    return card;
  }
  const valid = records.filter((record) => !record.problem);
  const skipped = records.filter((record) => record.problem);
  const start = h("button", { type: "button", class: "btn btn-sm btn-primary", disabled: !valid.length,
    text: `Import ${fmt.plural(valid.length, "row")}`, title: "Set these statuses and notes on the matching postings" });
  const progress = h("span", { class: "import-progress muted", "aria-live": "polite" });
  start.addEventListener("click", async () => {
    start.disabled = true;
    stop.running = true;
    close.title = "Stop after the rows in progress";
    const outcome = await importRecords(valid, stop, (n) => {
      progress.textContent = `Importing ${fmt.number(n)} of ${fmt.number(valid.length)}…`;
    });
    card.replaceChildren(...result(outcome, skipped, head, onClose).filter(Boolean));
    card.querySelector("button")?.focus();
  });
  card.replaceChildren(...[head(`Import ${fmt.plural(records.length, "row")}`),
    h("p", { class: "import-counts", text: previewCounts(valid, skipped) }),
    extra > 0 && h("p", { class: "tone-weak", text: `The file holds ${fmt.plural(records.length + extra, "row")}, but `
      + `only the first ${fmt.number(IMPORT_MAX)} will import. Split the file to import the rest.` }),
    h("div", { class: "table-wrap" }, h("table", { class: "data-table import-table" },
      h("thead", {}, h("tr", {}, ["Company", "Title", "Status", "Link"].map((label) => h("th", { scope: "col", text: label })))),
      h("tbody", {}, records.slice(0, PREVIEW_ROWS).map((record) => h("tr", {},
        h("td", { class: "cell-company", text: record.company || "–" }),
        h("td", { class: "cell-title", text: record.title || "–" }),
        h("td", { class: "cell-status" }, statusCell(record)),
        h("td", { class: "mono cell-link", text: linkText(record) })))))),
    records.length > PREVIEW_ROWS && h("p", { class: "muted", text: `and ${fmt.plural(records.length - PREVIEW_ROWS, "more row")}` }),
    h("div", { class: "import-actions" }, progress, close, start)].filter(Boolean));
  return card;
}

/** "3 new · 2 already tracked · 1 keeps its status · 2 new as Saved · 1 skipped" */
function previewCounts(valid, skipped) {
  const already = valid.filter((record) => record.tracked).length;
  const bare = valid.filter((record) => !record.status);
  const keep = bare.filter((record) => record.tracked).length;
  return [`${fmt.number(valid.length - already)} new`, `${fmt.number(already)} already tracked`,
    bare.length && `${fmt.number(keep)} ${keep === 1 ? "keeps its" : "keep their"} status`,
    bare.length && `${fmt.number(bare.length - keep)} new as Saved`,
    skipped.length && `${fmt.number(skipped.length)} skipped`].filter(Boolean).join(" · ");
}

function statusCell(record) {
  if (record.problem) return h("span", { class: "tone-weak", text: record.problem });
  const kept = !record.status && record.tracked;
  return [statusPill(kept ? record.tracked.status : record.status || DEFAULT_STATUS),
    record.tracked && h("span", { class: "import-tracked muted", text: kept ? "kept" : "tracked" })];
}

function linkText(record) {
  const url = safeUrl(record.url);
  return url ? new URL(url).host : record.id || record.url || "–";
}

function result(outcome, skipped, head, onClose) {
  const missed = [...outcome.unmatched.map((record) => `${describeRecord(record)} (no match)`),
    ...outcome.unchecked.map(({ record, reason }) => `${describeRecord(record)} (couldn't check: ${reason})`),
    ...outcome.unsaved.map(({ record, reason }) => `${describeRecord(record)} (couldn't save: ${reason})`),
    ...skipped.map((record) => `${describeRecord(record)} (line ${record.line}: ${record.problem})`)];
  const summary = [`Imported ${fmt.number(outcome.imported)}`,
    outcome.unchanged && `${fmt.number(outcome.unchanged)} unchanged`,
    missed.length && `${fmt.number(missed.length)} not imported`,
    outcome.notTried && `stopped with ${fmt.plural(outcome.notTried, "row")} not tried`].filter(Boolean).join(" · ");
  const list = missed.join("\n");
  return [head(outcome.notTried ? "Import stopped" : "Import finished"), h("p", { class: "import-counts", text: summary }),
    missed.length > 0 && h("div", { class: "import-missed" },
      h("p", { class: "section-label", text: "Not imported" }),
      h("p", { class: "muted", text: "A row matches a posting by id, or by url among the postings at its company." }),
      h("textarea", { class: "import-list mono", readonly: true, rows: String(Math.min(6, missed.length)),
        "aria-label": "Rows not imported", value: list })),
    h("div", { class: "import-actions" },
      missed.length > 0 && h("button", { type: "button", class: "btn btn-sm", text: "Copy list",
        title: "Copy the rows not imported", onclick: () => copyText(list, "the rows not imported") }),
      h("button", { type: "button", class: "btn btn-sm btn-primary", text: "Done", onclick: onClose }))];
}
